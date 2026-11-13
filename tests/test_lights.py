"""Light service: scenes, rooms, dimming, all on/off, locks, colour (in-process rig)."""

import asyncio
from datetime import datetime

import pytest

from app.clock import LOCAL_TZ
from tests.harness import Rig


def at(hh, mm=0, day=12):
    return datetime(2026, 11, day, hh, mm, tzinfo=LOCAL_TZ)


@pytest.fixture
def rig():
    r = Rig(at(20, 0))  # warm phase, master_ct = 1 -> 370 mired
    r.all_off_state()
    r.z2m.clear()
    return r


# --------------------------------------------------------------------------- room on / off


async def test_room_on_applies_current_phase_scene_with_colour(rig):
    await rig.lights.room_on("Wohnzimmer")
    bett = rig.z2m.to("wzBettlampe")[-1]
    assert bett == {"state": "ON", "brightness": 254, "color_temp": 370, "transition": 0.4}
    assert rig.z2m.to("wzTowerlampe")[-1] == {"state": "ON"}  # plug: no brightness/colour
    assert rig.lamp("wzDeckenlampe").is_on
    light = rig.store.room_light("Wohnzimmer")
    assert light.scene == "warm" and light.factor == 1.0


async def test_room_on_twice_resets_to_scene_incl_off(rig):
    rig.clock.set(at(21, 30))  # twilight
    await rig.lights.room_on("Wohnzimmer")
    await rig.lights.dim("Wohnzimmer", "down")
    await rig.lights.set_lamp("wzDeckenlampe", on=True, brightness=200)  # not in twilight scene
    rig.z2m.clear()
    await rig.lights.room_on("Wohnzimmer")
    assert rig.z2m.to("wzDeckenlampe")[-1]["state"] == "OFF"
    assert rig.lamp("wzBettlampe").brightness == round(0.37 * 254)
    assert rig.store.room_light("Wohnzimmer").factor == 1.0


async def test_room_off_only_in_room_off_lamps(rig):
    await rig.lights.room_on("Küche")
    await rig.lights.room_off("Küche")
    assert all(not rig.lamp(d.friendly_name).is_on for d in rig.cfg.lamps("Küche"))
    assert rig.z2m.to("kuecheWasserkocher") == []  # appliance, not a room lamp


# --------------------------------------------------------------------------- dimming


async def test_dim_down_and_up_keeps_ratios(rig):
    rig.clock.set(at(21, 30))  # twilight: Bett 37 %, Boden 45.7 %, Schreibtisch 48.4 %
    await rig.lights.room_on("Wohnzimmer")
    before = {n: rig.lamp(n).brightness for n in ("wzBettlampe", "wzBodenstrips")}
    rig.z2m.clear()
    assert await rig.lights.dim("Wohnzimmer", "down")
    after = {n: rig.lamp(n).brightness for n in before}
    assert after["wzBettlampe"] == round(before["wzBettlampe"] * 0.85)
    assert after["wzBodenstrips"] / after["wzBettlampe"] == pytest.approx(45.7 / 37, rel=0.03)
    assert rig.z2m.to("wzTowerlampe") == []  # plug ignores dimming (and is off in twilight)


async def test_dim_up_reaches_full_brightness_for_every_lamp(rig):
    """Decision 2026-11-13: the wall switch covers the full range of the lamps."""
    rig.clock.set(at(21, 30))
    await rig.lights.room_on("Wohnzimmer")
    while await rig.lights.dim("Wohnzimmer", "up"):
        pass
    for name in ("wzSchreibtischlampe", "wzBettlampe", "wzBodenstrips"):
        assert rig.lamp(name).brightness == 254, name


async def test_dim_down_reaches_minimum_for_every_lamp_and_back(rig):
    rig.clock.set(at(21, 30))
    await rig.lights.room_on("Wohnzimmer")
    scene = {n: rig.lamp(n).brightness for n in ("wzSchreibtischlampe", "wzBettlampe")}
    presses = 0
    while await rig.lights.dim("Wohnzimmer", "down"):
        presses += 1
    assert rig.lamp("wzSchreibtischlampe").brightness == 1  # the brightest one, too
    assert rig.lamp("wzBettlampe").brightness == 1 and rig.lamp("wzBettlampe").is_on
    assert presses > 20
    # going up again un-squeezes: the scene's ratio between the lamps comes back
    for _ in range(presses):
        await rig.lights.dim("Wohnzimmer", "up")
    a, b = (rig.lamp(n).brightness for n in scene)
    assert a / b == pytest.approx(scene["wzSchreibtischlampe"] / scene["wzBettlampe"], rel=0.05)


async def test_dim_room_with_nothing_on(rig):
    assert not await rig.lights.dim("Bad", "up")
    assert rig.z2m.sent == []


async def test_dim_lamp_turned_on_outside_scene_uses_its_level(rig):
    rig.z2m.report("badSpiegellampe", state="ON", brightness=100)
    await rig.lights.dim("Bad", "down")
    assert rig.lamp("badSpiegellampe").brightness == 85


# --------------------------------------------------------------------------- phase change


async def test_phase_change_fades_lit_rooms_only(rig):
    await rig.lights.room_on("Wohnzimmer")
    await rig.lights.room_on("Küche")
    rig.z2m.clear()
    await rig.lights.on_phase_change("twilight")
    # Wohnzimmer: Decke and Tower off, others to twilight levels with 10 s fade
    assert rig.z2m.to("wzDeckenlampe")[-1] == {"state": "OFF", "transition": 10.0}
    assert rig.z2m.to("wzTowerlampe")[-1] == {"state": "OFF"}
    assert rig.z2m.to("wzBettlampe")[-1]["brightness"] == round(0.37 * 254)
    assert rig.z2m.to("wzBettlampe")[-1]["transition"] == 10.0
    # Bad and Flur were off: untouched, but their active scene moves on
    assert rig.z2m.to("badDeckenlampe") == [] and rig.z2m.to("flurDeckenstrip") == []
    assert rig.store.room_light("Bad").scene == "twilight"


async def test_phase_change_turns_on_lamp_the_scene_wants(rig):
    """twilight -> night in a lit Küche: Tischlampe comes on at 100 % (decision 2026-11-12)."""
    rig.clock.set(at(21, 30))
    await rig.lights.room_on("Küche")
    assert not rig.lamp("kuecheTischlampe").is_on
    await rig.lights.on_phase_change("night")
    assert rig.lamp("kuecheTischlampe").is_on and rig.lamp("kuecheTischlampe").brightness == 254


async def test_phase_change_resets_dim_factor(rig):
    await rig.lights.room_on("Flur")
    await rig.lights.dim("Flur", "down")
    await rig.lights.on_phase_change("twilight")
    assert rig.store.room_light("Flur").factor == 1.0


# --------------------------------------------------------------------------- all on / off


async def test_all_off_uses_group_then_verifies(rig):
    await rig.lights.all_on()
    rig.z2m.drop.add("badSpiegellampe")  # misses the group command
    rig.z2m.clear()
    task = asyncio.create_task(rig.lights.all_off())
    await task
    assert rig.z2m.sent[0] == ("Alle_Lampen", {"state": "OFF", "transition": 0.4})
    assert rig.lamp("badSpiegellampe").is_on
    rig.z2m.drop.clear()
    await rig.clock.advance(2.1)
    await rig.lights.wait_idle()
    assert rig.z2m.to("badSpiegellampe")[-1]["state"] == "OFF"
    assert not rig.lamp("badSpiegellampe").is_on
    assert "pveSwitch" not in [t for t, _ in rig.z2m.sent]


async def test_all_on_lights_every_room(rig):
    await rig.lights.all_on()
    assert all(rig.lights.room_is_on(r) for r in rig.cfg.rooms)


# --------------------------------------------------------------------------- locks (sunrise)


async def test_room_lit_only_by_sunrise_counts_as_off_at_phase_change(rig):
    rig.z2m.report("wzBodenstrips", state="ON", brightness=20, color_temp=300)
    rig.lights.locked = {"wzBodenstrips", "wzSchreibtischlampe", "wzDeckenlampe"}
    rig.z2m.clear()
    await rig.lights.on_phase_change("kalt")
    assert rig.z2m.sent == []
    assert not rig.lamp("wzBettlampe").is_on


async def test_locked_lamps_are_never_touched(rig):
    locked = {"wzBodenstrips", "wzSchreibtischlampe", "wzDeckenlampe"}
    for name in locked:
        rig.z2m.report(name, state="ON", brightness=40, color_temp=300)
    rig.lights.locked = set(locked)
    rig.z2m.clear()
    await rig.lights.all_off()
    await rig.lights.all_on()
    await rig.lights.on_phase_change("kalt")
    await rig.lights.dim("Wohnzimmer", "up")
    await rig.lights.sync_colour()
    assert not await rig.lights.set_lamp("wzDeckenlampe", on=False)
    touched = {t for t, _ in rig.z2m.sent}
    assert not touched & locked
    assert "Alle_Lampen" not in touched  # all-off went per lamp because of the locks


# --------------------------------------------------------------------------- single lamp


async def test_set_lamp_brightness_updates_base(rig):
    await rig.lights.room_on("Flur")
    await rig.lights.set_lamp("flurDeckenlampe", brightness=127)
    assert rig.lamp("flurDeckenlampe").brightness == 127
    await rig.lights.dim("Flur", "down")
    assert rig.lamp("flurDeckenlampe").brightness == round(127 * 0.85)


async def test_set_lamp_on_carries_colour(rig):
    await rig.lights.set_lamp("kuecheTischlampe", on=True)
    assert rig.z2m.to("kuecheTischlampe")[-1]["color_temp"] == 370


async def test_set_lamp_refuses_non_lamps(rig):
    assert not await rig.lights.set_lamp("pveSwitch", on=True)
    assert not await rig.lights.set_lamp("kuecheWasserkocher", on=True)
    assert rig.z2m.sent == []


# --------------------------------------------------------------------------- colour


async def test_sync_colour_fixes_lamps_with_wrong_colour(rig):
    rig.z2m.report("wzDeckenlampe", state="ON", brightness=254, color_temp=451)  # Hue at 451 as seen live
    rig.z2m.report("wzBettlampe", state="ON", brightness=254, color_temp=370)  # already right
    sent = await rig.lights.sync_colour()
    assert sent == ["wzDeckenlampe"]
    assert rig.lamp("wzDeckenlampe").color_temp == 370
    assert rig.z2m.to("wzTowerlampe") == []


async def test_sync_colour_does_not_spam_lamps_that_dont_report(rig):
    rig.z2m.report("wzBettlampe", state="ON", color_temp=300)
    rig.z2m.drop.add("wzBettlampe")
    assert await rig.lights.sync_colour() == ["wzBettlampe"]
    assert await rig.lights.sync_colour() == []
    await rig.clock.advance(121)
    assert await rig.lights.sync_colour() == ["wzBettlampe"]


async def test_external_on_gets_colour_but_keeps_brightness(rig):
    rig.z2m.report("flurDeckenstrip", state="ON", brightness=50, color_temp=200)
    await rig.lights.wait_idle()
    assert rig.z2m.to("flurDeckenstrip") == [{"color_temp": 370, "transition": 0}]
    assert rig.lamp("flurDeckenstrip").brightness == 50


async def test_own_on_is_not_treated_as_external(rig):
    await rig.lights.room_on("Flur")
    await rig.lights.wait_idle()
    assert all("state" in p for p in rig.z2m.to("flurDeckenstrip"))  # no extra colour-only fix


async def test_dry_run_sends_nothing_and_skips_verify():
    rig = Rig(at(20, 0), dry_run=True)
    await rig.lights.all_on()
    await rig.lights.all_off()
    await rig.clock.advance(5)
    await rig.lights.wait_idle()
    assert rig.z2m.sent == []
    assert len(rig.publisher.history) > 10
