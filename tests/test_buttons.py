"""Wall switches, Flur hold timing and kettle timer (FakeClock, in-process rig)."""

import json
from datetime import datetime

import pytest

from app.clock import LOCAL_TZ
from app.services.buttons import ButtonService
from tests.harness import Rig


def at(hh, mm=0, ss=0):
    return datetime(2026, 11, 12, hh, mm, ss, tzinfo=LOCAL_TZ)


class ButtonRig(Rig):
    def __init__(self, start=at(20, 0), *, enabled=True, blocked=()):
        super().__init__(start)
        self.blocked = set(blocked)
        self.buttons = ButtonService(
            self.cfg, self.state, self.lights, self.publisher, self.store, self.clock,
            enabled=enabled, room_blocked=lambda room: room in self.blocked,
        )
        self.all_off_state()
        self.z2m.clear()

    async def press(self, name, action):
        self.state.handle_message(f"zigbee2mqtt/{name}", json.dumps({"action": action, "battery": 100}))
        await self.clock.advance(0)

    async def settle(self, seconds=0.0):
        # advance() lets every task that is due run; don't wait for tasks that
        # sleep on the fake clock (they would wait forever).
        await self.clock.advance(seconds)


@pytest.fixture
def rig():
    return ButtonRig()


# --------------------------------------------------------------------------- basic switch


async def test_switches_mapped_to_rooms(rig):
    assert rig.buttons.switch_room == {
        "wzLichtschalter": "Wohnzimmer", "kuecheLichtschalter": "Küche",
        "badLichtschalter": "Bad", "flurLichtschalter": "Flur",
    }


async def test_top_bottom_up_down(rig):
    await rig.press("badLichtschalter", "on_press")
    assert rig.lights.room_is_on("Bad")
    level = rig.lamp("badSpiegellampe").brightness
    await rig.press("badLichtschalter", "down_press")
    assert rig.lamp("badSpiegellampe").brightness == round(level * 0.85)
    await rig.press("badLichtschalter", "up_press")
    await rig.press("badLichtschalter", "off_press")
    assert not rig.lights.room_is_on("Bad")


async def test_top_in_lit_room_resets_scene(rig):
    await rig.press("kuecheLichtschalter", "on_press")
    await rig.press("kuecheLichtschalter", "down_press")
    await rig.press("kuecheLichtschalter", "on_press")
    assert rig.lamp("kuecheDeckenlampe").brightness == 254
    assert rig.store.room_light("Küche").factor == 1.0


async def test_holds_releases_and_recall_are_ignored(rig):
    for action in ("up_hold", "up_hold_release", "down_press_release", "recall_0", "on_press_release"):
        await rig.press("badLichtschalter", action)
    assert rig.z2m.sent == []


async def test_disabled_buttons_do_nothing(caplog):
    rig = ButtonRig(enabled=False)
    with caplog.at_level("INFO"):
        await rig.press("flurLichtschalter", "on_press")
        await rig.press("kuecheButton", "press")
        await rig.settle(5)
    assert rig.z2m.sent == []
    assert "BUTTONS_ENABLED=false" in caplog.text


async def test_unrelated_remotes_are_ignored(rig):
    await rig.press("wzBettdialswitch", "brightness_step_up")
    await rig.press("wzTowerbutton", "press")
    assert rig.z2m.sent == []


async def test_blocked_room_ignores_switch_but_others_work():
    rig = ButtonRig(blocked={"Wohnzimmer"})
    await rig.press("wzLichtschalter", "on_press")
    await rig.press("wzLichtschalter", "off_press")
    await rig.press("kuecheLichtschalter", "on_press")
    assert not rig.lights.room_is_on("Wohnzimmer")
    assert rig.lights.room_is_on("Küche")


# --------------------------------------------------------------------------- Flur hold


async def test_flur_short_press_only_room(rig):
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(0.3)
    await rig.press("flurLichtschalter", "on_press_release")
    await rig.settle(3)
    assert rig.lights.room_is_on("Flur")
    assert not rig.lights.room_is_on("Küche")


async def test_flur_hold_top_all_on(rig):
    await rig.press("flurLichtschalter", "on_press")
    assert rig.lights.room_is_on("Flur")  # immediate
    await rig.settle(0.8)
    await rig.press("flurLichtschalter", "on_hold")
    await rig.settle(0.69)
    assert not rig.lights.room_is_on("Küche")  # not yet at 1.49 s
    await rig.settle(0.02)
    assert all(rig.lights.room_is_on(r) for r in rig.cfg.rooms)


async def test_flur_hold_bottom_all_off(rig):
    await rig.lights.all_on()
    rig.z2m.clear()
    await rig.press("flurLichtschalter", "off_press")
    await rig.settle(0.8)
    await rig.press("flurLichtschalter", "off_hold")
    await rig.settle(0.8)
    await rig.settle(2.5)  # verification pass
    assert ("Alle_Lampen", {"state": "OFF", "transition": 0.4}) in rig.z2m.sent
    assert not any(rig.lights.room_is_on(r) for r in rig.cfg.rooms)


async def test_flur_released_before_1_5_s_no_all_on(rig):
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(0.8)
    await rig.press("flurLichtschalter", "on_hold")
    await rig.settle(0.5)
    await rig.press("flurLichtschalter", "on_hold_release")  # let go at 1.3 s
    await rig.settle(2)
    assert not rig.lights.room_is_on("Küche")


async def test_flur_lost_release_without_hold_is_not_a_hold(rig):
    """Release message lost, no hold message either -> never infer 'held'."""
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(3)
    assert rig.lights.room_is_on("Flur")
    assert not rig.lights.room_is_on("Küche")


async def test_flur_late_hold_within_grace_still_counts(rig):
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(1.7)  # hold message delayed by the network
    await rig.press("flurLichtschalter", "on_hold")
    await rig.settle(0.1)
    assert rig.lights.room_is_on("Küche")


async def test_flur_hold_after_grace_is_ignored(rig):
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(2.2)
    await rig.press("flurLichtschalter", "on_hold")
    await rig.settle(1)
    assert not rig.lights.room_is_on("Küche")


async def test_flur_hold_of_other_button_does_not_count(rig):
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(0.8)
    await rig.press("flurLichtschalter", "off_hold")
    await rig.settle(1)
    assert not rig.lights.room_is_on("Küche")


async def test_hold_rule_only_on_flur(rig):
    await rig.press("kuecheLichtschalter", "on_press")
    await rig.settle(0.8)
    await rig.press("kuecheLichtschalter", "on_hold")
    await rig.settle(1)
    assert rig.lights.room_is_on("Küche") and not rig.lights.room_is_on("Bad")


async def test_flur_all_on_respects_sunrise_lock(rig):
    rig.lights.locked = {"wzDeckenlampe"}
    await rig.press("flurLichtschalter", "on_press")
    await rig.settle(0.8)
    await rig.press("flurLichtschalter", "on_hold")
    await rig.settle(1)
    assert rig.z2m.to("wzDeckenlampe") == []
    assert rig.lamp("wzBettlampe").is_on


# --------------------------------------------------------------------------- kettle


async def test_kettle_on_then_off_after_180_s(rig):
    await rig.press("kuecheButton", "on")
    await rig.press("kuecheButton", "press")  # same physical press
    assert rig.z2m.to("kuecheWasserkocher") == [{"state": "ON"}]
    await rig.settle(179)
    assert rig.lamp("kuecheWasserkocher").is_on
    await rig.settle(1.1)
    assert rig.z2m.to("kuecheWasserkocher") == [{"state": "ON"}, {"state": "OFF"}]
    assert rig.store.get("kettle_off_at") is None


async def test_kettle_second_press_switches_off(rig):
    """Decision 2026-11-12: a press while the kettle runs stops it."""
    await rig.press("kuecheButton", "press")
    await rig.press("kuecheButton", "release")
    await rig.press("kuecheButton", "on")
    await rig.settle(5)  # the live sequence for one press, seen on the real button
    assert rig.lamp("kuecheWasserkocher").is_on
    await rig.press("kuecheButton", "press")
    await rig.press("kuecheButton", "off")
    assert not rig.lamp("kuecheWasserkocher").is_on
    assert rig.store.get("kettle_off_at") is None
    await rig.settle(200)  # cancelled timer sends nothing more
    assert rig.z2m.to("kuecheWasserkocher") == [{"state": "ON"}, {"state": "OFF"}]
    await rig.press("kuecheButton", "press")  # and a later press starts it again
    assert rig.lamp("kuecheWasserkocher").is_on


async def test_kettle_timer_survives_restart(rig):
    await rig.press("kuecheButton", "press")
    await rig.settle(60)
    stored = rig.store.get("kettle_off_at")
    assert stored

    # "restart": new services on the same store, 2 minutes later
    later = ButtonRig(start=at(20, 2, 30))
    later.store = rig.store
    later.buttons.store = rig.store
    later.buttons.resume()
    await later.settle(29)
    assert later.z2m.to("kuecheWasserkocher") == []
    await later.settle(2)
    assert later.z2m.to("kuecheWasserkocher") == [{"state": "OFF"}]


async def test_kettle_overdue_after_restart_switches_off_immediately(rig):
    await rig.press("kuecheButton", "press")
    later = ButtonRig(start=at(21, 0))
    later.store = rig.store
    later.buttons.store = rig.store
    later.buttons.resume()
    await later.settle(0)
    assert later.z2m.to("kuecheWasserkocher") == [{"state": "OFF"}]


async def test_kettle_off_retries_until_sent(rig):
    await rig.press("kuecheButton", "press")
    rig.publisher.set_transport(None)  # MQTT down when the timer fires
    await rig.settle(181)
    assert rig.store.get("kettle_off_at") is not None
    rig.publisher.set_transport(rig.z2m)
    await rig.settle(5)
    assert rig.z2m.to("kuecheWasserkocher")[-1] == {"state": "OFF"}


async def test_kettle_timer_completes_even_if_buttons_get_disabled(rig):
    await rig.press("kuecheButton", "press")
    rig.buttons.enabled = False
    await rig.settle(181)
    assert rig.z2m.to("kuecheWasserkocher")[-1] == {"state": "OFF"}
