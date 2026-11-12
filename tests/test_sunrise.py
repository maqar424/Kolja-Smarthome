"""Sunrise runner end to end (FakeClock + in-process echo Z2M).

The wake-up must be dependable, so every branch is covered here.
"""

import asyncio
from datetime import date, datetime

import pytest

from app.clock import LOCAL_TZ
from app.domain.sunrise import LAMP_NAMES
from app.services.buttons import ButtonService
from app.services.schedule import Schedule
from app.services.sunrise import SunriseService
from app.services.lights import LightService
from app.services.scheduler import Scheduler
from tests.harness import Rig


def at(hh, mm=0, ss=0, day=13):
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=LOCAL_TZ)


class SunRig(Rig):
    """Rig with sunrise, schedule wired for early day start, buttons enabled."""

    def __init__(self, start, *, store=None, connected=True, synced=True):
        super().__init__(start)
        if store is not None:
            self.store = store
        self.connected = connected
        self.state.bridge_online = True
        self.schedule = Schedule(self.store, self.clock, wakes=lambda: self.sunrise.early_starts())
        self.lights = LightService(
            self.cfg, self.state, self.publisher, self.store, self.clock,
            phase=self.schedule.phase, mired=self.schedule.mired,
        )
        self.scheduler = Scheduler(self.schedule, self.lights, self.store, self.clock)
        self.sunrise = SunriseService(
            self.cfg, self.state, self.lights, self.publisher, self.store, self.clock,
            connected=lambda: self.connected, clock_synced=lambda: synced,
        )
        self.sunrise.on_schedule_change = self.schedule.changed.set
        self.sunrise.on_final_applied = self.scheduler.mark_current_phase_applied
        self.buttons = ButtonService(
            self.cfg, self.state, self.lights, self.publisher, self.store, self.clock,
            enabled=True, room_blocked=self.sunrise.room_blocked,
        )
        self.tasks = []
        if store is None:
            self.all_off_state()
            self.z2m.clear()

    def arm(self, wake="06:30", duration=30):
        assert self.sunrise.update_settings({"enabled": True, "wake": wake, "duration_min": duration}) == []

    async def start(self, scheduler=True):
        self.tasks.append(asyncio.create_task(self.sunrise.run()))
        if scheduler:
            self.tasks.append(asyncio.create_task(self.scheduler.run()))
        await self.clock.advance(0)

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks.clear()

    def record(self, day="2026-11-13"):
        return self.store.get("sunrise_runs", {}).get(day)


@pytest.fixture
async def rig():
    r = SunRig(at(5, 0))
    yield r
    await r.stop()


# --------------------------------------------------------------------------- normal run


async def test_normal_run(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(5, 59, 59))
    assert rig.z2m.to("wzBodenstrips") == []

    await rig.clock.advance(to=at(6, 0, 1))
    first = rig.z2m.to("wzBodenstrips")[0]
    assert first == {"state": "ON", "brightness": 1, "color_temp": 370, "transition": 0}
    assert rig.lights.locked == set(LAMP_NAMES)
    assert not rig.lamp("wzSchreibtischlampe").is_on

    await rig.clock.advance(to=at(6, 10, 1))  # t = 1/3
    assert rig.lamp("wzSchreibtischlampe").is_on
    assert not rig.lamp("wzDeckenlampe").is_on
    await rig.clock.advance(to=at(6, 20, 1))  # t = 2/3
    assert rig.lamp("wzDeckenlampe").is_on

    # brightness only ever rises, colour only ever gets colder, steps every 5 s with 5 s transitions
    levels = [p["brightness"] for p in rig.z2m.to("wzBodenstrips")]
    assert levels == sorted(levels)
    colours = [p["color_temp"] for p in rig.z2m.to("wzBodenstrips")]
    assert colours == sorted(colours, reverse=True)
    assert all(p["transition"] == 5.0 for p in rig.z2m.to("wzBodenstrips")[1:])

    await rig.clock.advance(to=at(6, 30, 10))
    # final alle_kalt: every room in kalt, coldest colour (early day start), locks released
    assert rig.lights.locked == set()
    for room in rig.cfg.rooms:
        assert rig.lights.room_is_on(room)
    assert rig.lamp("wzBettlampe").brightness == 254 and rig.lamp("wzBettlampe").color_temp == 153
    assert not rig.lamp("wzTowerlampe").is_on  # kalt scene: plug off
    assert rig.record()["status"] == "done" and rig.record()["problems"] == []
    assert rig.sunrise.warning() is None  # success shows nothing


async def test_early_day_start(rig):
    rig.arm("06:15", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 15, 5))
    tl = rig.schedule.timeline()
    assert tl.phase == "kalt"
    assert tl.last_boundary(rig.clock.now()).source == "wake"
    assert rig.schedule.colour().master_ct == 0.0


async def test_wake_after_kalt_does_not_move_day(rig):
    rig.arm("07:45", 30)
    await rig.start()
    await rig.clock.advance(to=at(7, 0, 5))
    b = rig.schedule.timeline().last_boundary(rig.clock.now())
    assert (b.phase, b.source) == ("kalt", "schedule")


async def test_phase_boundary_during_animation_does_not_touch_room(rig):
    """kalt starts 07:00 while the animation runs 06:45-07:15: other Schlafzimmer lamps stay off."""
    rig.arm("07:15", 30)
    await rig.start()
    await rig.clock.advance(to=at(7, 5))
    assert rig.store.applied_phase()["phase"] == "kalt"
    assert not rig.lamp("wzBettlampe").is_on
    assert rig.lights.locked == set(LAMP_NAMES)


# --------------------------------------------------------------------------- fallback


async def test_fallback_when_schlafzimmer_already_lit(rig):
    rig.z2m.report("wzBettlampe", state="ON", brightness=20, color_temp=370)
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 10))
    assert rig.record()["mode"] == "fallback"
    assert rig.z2m.to("wzBodenstrips") == []  # no animation
    assert rig.lights.locked == set()

    rig.z2m.report("wzDeckenlampe", state="ON", brightness=254, color_temp=153)  # already in final state
    await rig.clock.advance(1)
    rig.z2m.clear()
    await rig.clock.advance(to=at(6, 30, 10))
    # always sent, even though the lamp already shows exactly this state
    assert rig.z2m.to("wzDeckenlampe")[0] == {"state": "ON", "brightness": 254, "color_temp": 153, "transition": 3}
    assert all(rig.lights.room_is_on(r) for r in rig.cfg.rooms)
    assert rig.lamp("wzDeckenlampe").brightness == 254
    assert rig.record()["status"] == "done"


async def test_fallback_counts_plugs_as_lit(rig):
    rig.z2m.report("wzLavalampe", state="ON")
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 1))
    assert rig.record()["mode"] == "fallback"


async def test_fallback_ceiling_stays_100_even_if_kalt_were_lower(rig, monkeypatch):
    from app.config import SceneValue
    monkeypatch.setitem(rig.cfg.brightness["kalt"], "wzDeckenlampe", SceneValue(True, 30.0))
    rig.z2m.report("wzBettlampe", state="ON")
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 30, 10))
    assert rig.lamp("wzDeckenlampe").brightness == 254
    monkeypatch.undo()


# --------------------------------------------------------------------------- no interference / cancel


async def test_wall_switch_ignored_during_animation_other_rooms_work(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 15))
    rig.z2m.clear()
    for action in ("off_press", "on_press", "down_press"):
        rig.state.handle_message("zigbee2mqtt/wzLichtschalter", f'{{"action":"{action}"}}')
    rig.state.handle_message("zigbee2mqtt/badLichtschalter", '{"action":"on_press"}')
    await rig.clock.advance(1)
    assert not any(t in ("wzBettlampe",) or (t in LAMP_NAMES and "state" in p) for t, p in rig.z2m.sent)
    assert rig.lights.room_is_on("Bad")


async def test_flur_all_off_leaves_animated_lamps(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 25))
    await rig.lights.all_off()
    await rig.clock.advance(3)
    assert all(rig.lamp(n).is_on for n in LAMP_NAMES)


async def test_cancel_stops_animation_and_final(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 12))
    level = rig.lamp("wzBodenstrips").brightness
    assert rig.sunrise.cancel()
    await rig.clock.advance(to=at(6, 40))
    assert rig.lamp("wzBodenstrips").brightness == level  # stays as it is
    assert not rig.lights.room_is_on("Bad")  # no alle_kalt
    assert rig.record()["status"] == "cancelled"
    assert rig.lights.locked == set()
    assert rig.sunrise.settings.enabled  # toggle stays on


async def test_cancel_also_cancels_early_day_start(rig):
    """Decision 2026-11-12 (b): a cancelled sunrise does not start the day early."""
    rig.arm("06:15", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 0))
    rig.sunrise.cancel()
    await rig.clock.advance(to=at(6, 30))
    assert rig.schedule.timeline().phase == "night"
    await rig.clock.advance(to=at(7, 0, 5))
    assert rig.schedule.timeline().phase == "kalt"


async def test_runs_again_next_day_after_cancel(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 10))
    rig.sunrise.cancel()
    rig.all_off_state()
    await rig.clock.advance(to=at(6, 1, day=14))
    assert rig.record("2026-11-14")["status"] == "running"


async def test_toggle_off_never_runs(rig):
    rig.arm("06:30", 30)
    rig.sunrise.update_settings({"enabled": False})
    await rig.start()
    await rig.clock.advance(to=at(7, 0))
    assert rig.z2m.sent == [] or all(t not in LAMP_NAMES for t, _ in rig.z2m.sent)
    assert rig.record() is None


async def test_cancel_without_run_returns_false(rig):
    assert rig.sunrise.cancel() is False


# --------------------------------------------------------------------------- restart


async def test_restart_mid_animation_resumes_at_right_point(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 12))
    await rig.stop()  # app dies

    later = SunRig(at(6, 21), store=rig.store)
    for name in ("wzBodenstrips", "wzSchreibtischlampe"):  # lamps keep their state
        later.z2m.report(name, state="ON", brightness=40, color_temp=300)
    try:
        await later.start()
        assert later.record()["mode"] == "animation"  # not mistaken for "already lit"
        assert later.lights.locked == set(LAMP_NAMES)
        await later.clock.advance(6)
        assert later.lamp("wzDeckenlampe").is_on  # t > 2/3 now
        await later.clock.advance(to=at(6, 30, 10))
        assert later.record()["status"] == "done"
        assert later.lights.room_is_on("Flur")
    finally:
        await later.stop()


async def test_restart_after_wake_within_duration_applies_final(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 20))
    await rig.stop()
    later = SunRig(at(6, 50), store=rig.store)
    try:
        await later.start()
        await later.clock.advance(5)
        assert later.record()["status"] == "done"
        assert all(later.lights.room_is_on(r) for r in later.cfg.rooms)
    finally:
        await later.stop()


async def test_app_down_during_whole_animation_still_applies_final(rig):
    rig.arm("06:30", 30)
    later = SunRig(at(6, 40), store=rig.store)
    try:
        later.all_off_state()
        await later.start()
        await later.clock.advance(5)
        assert later.record()["status"] == "done"
        assert later.lights.room_is_on("Küche")
    finally:
        await later.stop()


async def test_restart_too_late_marks_missed_with_warning(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 20))
    await rig.stop()
    later = SunRig(at(9, 0), store=rig.store)
    try:
        await later.start()
        assert later.record()["status"] == "missed"
        assert "nicht aktiv" in later.sunrise.warning()["problems"][0]
        assert not later.lights.room_is_on("Küche")  # no surprise alle_kalt at 9:00
    finally:
        await later.stop()


# --------------------------------------------------------------------------- problems -> warning


async def test_lamp_not_responding_gives_warning(rig):
    rig.arm("06:30", 30)
    rig.z2m.drop.add("badDeckenlampe")
    await rig.start()
    await rig.clock.advance(to=at(6, 30, 10))
    w = rig.sunrise.warning()
    assert w and any("badDeckenlampe" in p for p in w["problems"])
    # it was resent individually before giving up
    assert len(rig.z2m.to("badDeckenlampe")) >= 2
    rig.sunrise.dismiss_warning()
    assert rig.sunrise.warning() is None


async def test_start_lamp_not_responding_gives_warning(rig):
    rig.arm("06:30", 30)
    rig.z2m.drop.add("wzBodenstrips")
    await rig.start()
    await rig.clock.advance(to=at(6, 30, 10))
    assert any("wzBodenstrips" in p for p in rig.sunrise.warning()["problems"])


async def test_z2m_offline_and_unsynced_clock_are_reported():
    rig = SunRig(at(5, 0), synced=False)
    rig.state.bridge_online = False
    rig.arm("06:30", 30)
    await rig.start()
    try:
        await rig.clock.advance(to=at(6, 30, 10))
        problems = rig.sunrise.warning()["problems"]
        assert any("offline" in p for p in problems) and any("Systemuhr" in p for p in problems)
    finally:
        await rig.stop()


async def test_mqtt_down_at_wake_waits_and_then_applies(rig):
    rig.arm("06:30", 30)
    await rig.start()
    await rig.clock.advance(to=at(6, 29, 50))
    rig.connected = False
    rig.publisher.set_transport(None)
    await rig.clock.advance(to=at(6, 30, 30))
    assert rig.record()["status"] == "running"  # waiting for MQTT
    rig.connected = True
    rig.publisher.set_transport(rig.z2m)
    await rig.clock.advance(10)
    assert rig.record()["status"] == "done"
    assert rig.lights.room_is_on("Küche")
    assert any("MQTT" in p for p in rig.sunrise.warning()["problems"])


# --------------------------------------------------------------------------- test button


async def test_test_mode_plays_compressed_sequence(rig):
    rig.clock.set(at(14, 0))
    await rig.start()
    assert rig.sunrise.start_test()
    assert not rig.sunrise.start_test()  # one at a time
    await rig.clock.advance(20)
    assert rig.lamp("wzSchreibtischlampe").is_on
    await rig.clock.advance(31)
    assert all(rig.lights.room_is_on(r) for r in rig.cfg.rooms)
    assert rig.lamp("kuecheDeckenlampe").color_temp == 153  # morning look, though it's 14:00 (master 0 anyway)
    await rig.clock.advance(9)
    assert rig.store.get("sunrise_runs")["test"]["status"] == "done"
    assert rig.sunrise.early_starts() == {}  # a test never moves the day start
    assert rig.lights.locked == set()


async def test_test_mode_in_evening_shows_cold_final_then_glides_back(rig):
    rig.clock.set(at(21, 30, day=12))  # twilight, master colour warm (370)
    await rig.start()
    rig.sunrise.start_test()
    await rig.clock.advance(52)
    assert rig.lamp("badDeckenlampe").color_temp == 153  # final shows the morning colour
    await rig.clock.advance(60)
    assert rig.lamp("badDeckenlampe").color_temp == 370  # colour sync brings the evening back


async def test_test_mode_ignores_already_lit_room(rig):
    rig.z2m.report("wzBettlampe", state="ON")
    await rig.start()
    rig.sunrise.start_test()
    await rig.clock.advance(5)
    assert rig.lights.locked == set(LAMP_NAMES)


async def test_status_for_ui(rig):
    rig.arm("06:30", 30)
    s = rig.sunrise.status()
    assert s["settings"] == {"enabled": True, "wake": "06:30", "duration_min": 30}
    assert s["next"]["wake"].startswith("2026-11-13T06:30")
    assert s["running"] is False and s["warning"] is None
