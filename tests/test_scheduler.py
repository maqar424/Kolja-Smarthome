"""Scheduler loop over simulated hours (FakeClock), incl. restart behaviour."""

import asyncio
from datetime import datetime

import pytest

from app.clock import LOCAL_TZ
from app.domain.colour import to_mired
from tests.harness import Rig


def at(hh, mm=0, day=12):
    return datetime(2026, 11, day, hh, mm, tzinfo=LOCAL_TZ)


async def start(rig):
    task = asyncio.create_task(rig.scheduler.run())
    await rig.clock.advance(0)
    return task


async def stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture
def rig():
    r = Rig(at(17, 0))  # kalt, colour 0 (153)
    r.all_off_state()
    return r


async def test_first_start_touches_no_lamp(rig):
    rig.z2m.clear()
    task = await start(rig)
    try:
        assert rig.store.applied_phase()["phase"] == "kalt"
        assert rig.z2m.sent == []
    finally:
        await stop(task)


async def test_evening_runs_through_all_phases(rig):
    task = await start(rig)
    try:
        await rig.lights.room_on("Wohnzimmer")
        assert rig.lamp("wzBettlampe").color_temp == 153
        await rig.clock.advance(to=at(18, 0, ))
        await rig.lights.wait_idle()
        assert rig.store.applied_phase()["phase"] == "warm"
        assert rig.lamp("wzTowerlampe").is_on  # warm scene turns the plug on
        # colour ramps up over 60 min, lamps follow
        await rig.clock.advance(to=at(18, 30))
        assert abs(rig.lamp("wzBettlampe").color_temp - to_mired(0.5)) <= 2
        await rig.clock.advance(to=at(19, 1))
        assert rig.lamp("wzBettlampe").color_temp == 370
        await rig.clock.advance(to=at(21, 0, ))
        assert rig.store.applied_phase()["phase"] == "twilight"
        assert not rig.lamp("wzDeckenlampe").is_on
        assert rig.lamp("wzBettlampe").brightness == round(0.37 * 254)
        await rig.clock.advance(to=at(22, 30))
        assert rig.lamp("wzBettlampe").brightness == round(0.051 * 254)
        assert not rig.lamp("badDeckenlampe").is_on  # rooms that were off stay off
    finally:
        await stop(task)


async def test_colour_steps_are_small_and_smooth(rig):
    task = await start(rig)
    try:
        await rig.lights.room_on("Flur")
        await rig.clock.advance(to=at(18, 0))
        rig.z2m.clear()
        await rig.clock.advance(to=at(19, 0))
        steps = [p for p in rig.z2m.to("flurDeckenstrip") if "color_temp" in p]
        values = [p["color_temp"] for p in steps]
        assert values == sorted(values)  # monotonic warmer
        assert max(b - a for a, b in zip(values, values[1:])) <= 5  # small steps
        assert all(p["transition"] == 20.0 for p in steps)
        # at most one step per 30 s tick (120), fewer when the change is within 1 mired
        assert 80 <= len(steps) <= 120
    finally:
        await stop(task)


async def test_manual_advance_reacts_immediately(rig):
    task = await start(rig)
    try:
        await rig.lights.room_on("Küche")
        assert rig.schedule.advance() == "warm"
        await rig.clock.advance(0)
        await rig.lights.wait_idle()
        assert rig.store.applied_phase()["phase"] == "warm"
        # colour jumped to warm in the same fade, not a 60-min ramp
        assert rig.lamp("kuecheDeckenlampe").color_temp == 370
        # scheduled warm at 18:00 is skipped: nothing re-applied
        rig.z2m.clear()
        await rig.clock.advance(to=at(18, 5))
        assert not any("state" in p for _, p in rig.z2m.sent)
    finally:
        await stop(task)


async def test_advance_in_night_does_nothing(rig):
    rig.clock.set(at(23, 0))
    assert rig.schedule.advance() is None


async def test_override_holds_until_warm_ramp(rig):
    task = await start(rig)
    try:
        await rig.lights.room_on("Bad")
        rig.schedule.set_override(0.5)
        await rig.clock.advance(1)
        assert rig.lamp("badSpiegellampe").color_temp == to_mired(0.5)
        await rig.clock.advance(to=at(17, 50))
        assert rig.lamp("badSpiegellampe").color_temp == to_mired(0.5)
        await rig.clock.advance(to=at(19, 1))
        assert rig.lamp("badSpiegellampe").color_temp == 370
        assert rig.store.colour_override() is not None  # stored, but superseded by the ramp
        assert not rig.schedule.colour().override
    finally:
        await stop(task)


async def test_back_to_automatic(rig):
    task = await start(rig)
    try:
        await rig.lights.room_on("Bad")
        rig.schedule.set_override(1.0)
        await rig.clock.advance(1)
        rig.schedule.clear_override()
        await rig.clock.advance(1)
        assert rig.lamp("badSpiegellampe").color_temp == 153
    finally:
        await stop(task)


async def test_settings_edit_applies_immediately(rig):
    task = await start(rig)
    try:
        await rig.lights.room_on("Flur")
        assert rig.schedule.update_settings({"warm": "16:30"}) == []
        await rig.clock.advance(0)
        await rig.lights.wait_idle()
        assert rig.store.applied_phase()["phase"] == "warm"
        assert rig.schedule.update_settings({"warm": "05:00"})  # invalid order -> errors, not saved
        assert rig.schedule.settings.warm.hour == 16
    finally:
        await stop(task)


async def test_restart_after_missed_boundary_catches_up(rig):
    task = await start(rig)
    await rig.lights.room_on("Wohnzimmer")
    await stop(task)  # app goes down at 17:00 (kalt)

    rig.clock.set(at(21, 10))  # back up during twilight
    task = await start(rig)
    try:
        await rig.lights.wait_idle()
        assert rig.store.applied_phase()["phase"] == "twilight"
        assert not rig.lamp("wzDeckenlampe").is_on
        assert rig.lamp("wzBettlampe").color_temp == 370
    finally:
        await stop(task)


async def test_restart_mid_ramp_resumes_colour(rig):
    task = await start(rig)
    await rig.lights.room_on("Flur")
    await rig.clock.advance(to=at(18, 10))
    await stop(task)
    rig.clock.set(at(18, 40))
    task = await start(rig)
    try:
        assert abs(rig.lamp("flurDeckenstrip").color_temp - to_mired(40 / 60)) <= 2
    finally:
        await stop(task)


async def test_lamp_powered_on_later_gets_corrected_by_next_tick(rig):
    """Even if the external-on fix were lost, the periodic sync catches it."""
    task = await start(rig)
    try:
        rig.z2m.drop.add("kuecheTwintowers")
        rig.z2m.report("kuecheTwintowers", state="ON", brightness=100, color_temp=300)
        await rig.clock.advance(1)
        rig.z2m.drop.clear()
        await rig.clock.advance(150)
        assert rig.lamp("kuecheTwintowers").color_temp == 153
    finally:
        await stop(task)
