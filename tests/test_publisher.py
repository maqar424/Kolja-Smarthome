"""The publish chokepoint enforces the hard rules. These tests are the safety net."""

import json
from datetime import datetime

import pytest

from app.clock import FakeClock
from app.config import AppConfig
from app.mqtt.publisher import Publisher


class RecordingTransport:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def __call__(self, topic: str, text: str) -> None:
        self.sent.append((topic, json.loads(text)))


@pytest.fixture(scope="module")
def config():
    return AppConfig.load()


@pytest.fixture
def transport():
    return RecordingTransport()


@pytest.fixture
def pub(config, transport):
    clock = FakeClock(datetime(2026, 11, 12, 20, 0))
    return Publisher(config, clock, base_topic="zigbee2mqtt", dry_run=False, transport=transport)


# --------------------------------------------------------------------------- accepted


async def test_lamp_on_with_brightness_and_colour(pub, transport):
    assert await pub.send("wzBettlampe", {"state": "on", "brightness": 127, "color_temp": 300, "transition": 2})
    assert transport.sent == [
        ("zigbee2mqtt/wzBettlampe/set", {"state": "ON", "brightness": 127, "color_temp": 300, "transition": 2})
    ]


async def test_plug_on_off(pub, transport):
    assert await pub.send("wzTowerlampe", {"state": "ON"})
    assert await pub.send("kuecheWasserkocher", {"state": "OFF"})
    assert [t for t, _ in transport.sent] == ["zigbee2mqtt/wzTowerlampe/set", "zigbee2mqtt/kuecheWasserkocher/set"]


async def test_group_off(pub, transport):
    assert await pub.send("Alle_Lampen", {"state": "OFF", "transition": 1})
    assert await pub.send("Küche", {"state": "OFF"})
    assert len(transport.sent) == 2


async def test_values_are_clamped(pub, transport):
    await pub.send("wzDeckenlampe", {"brightness": 0, "color_temp": 454})
    await pub.send("wzDeckenlampe", {"brightness": 400.6, "color_temp": 100})
    await pub.send("wzDeckenlampe", {"state": "ON", "transition": 999})
    assert transport.sent[0][1] == {"brightness": 1, "color_temp": 370}
    assert transport.sent[1][1] == {"brightness": 254, "color_temp": 153}
    assert transport.sent[2][1] == {"state": "ON", "transition": 60}


# --------------------------------------------------------------------------- refused (hard rules)


@pytest.mark.parametrize(
    "payload",
    [{"state": "ON"}, {"state": "OFF"}, {"state": "TOGGLE"}],
)
async def test_pveswitch_is_never_commanded(pub, transport, payload):
    assert not await pub.send("pveSwitch", payload)
    assert transport.sent == []
    assert pub.refused_count == 1


async def test_pveswitch_refused_even_in_dry_run(config):
    pub = Publisher(config, FakeClock(datetime(2026, 11, 12)), base_topic="zigbee2mqtt", dry_run=True)
    assert not await pub.send("pveSwitch", {"state": "OFF"})
    assert list(pub.history) == []


@pytest.mark.parametrize(
    "target,payload",
    [
        ("Wohnzimmer", {"scene_recall": 1}),
        ("Alle_Lampen", {"scene_store": 1}),
        ("wzBettlampe", {"scene_recall": 1}),
        ("kuecheTischlampe", {"color": {"x": 0.3, "y": 0.3}}),
        ("kuecheTischlampe", {"hue": 100}),
        ("wzBettlampe", {"effect": "blink"}),
        ("wzBettlampe", {"brightness_step": 10}),
    ],
)
async def test_forbidden_payload_keys(pub, transport, target, payload):
    assert not await pub.send(target, payload)
    assert transport.sent == []


@pytest.mark.parametrize(
    "target,payload",
    [
        ("wzTowerlampe", {"state": "ON", "brightness": 100}),
        ("wzLavalampe", {"color_temp": 300}),
        ("kuecheWasserkocher", {"brightness": 10}),
    ],
)
async def test_plugs_never_get_brightness_or_colour(pub, transport, target, payload):
    assert not await pub.send(target, payload)
    assert transport.sent == []


@pytest.mark.parametrize(
    "payload",
    [{"color_temp": 300}, {"brightness": 100}, {"state": "ON"}, {"state": "ON", "brightness": 100}],
)
async def test_groups_only_accept_off(pub, transport, payload):
    assert not await pub.send("Alle_Lampen", payload)
    assert not await pub.send("Wohnzimmer", payload)
    assert transport.sent == []


@pytest.mark.parametrize(
    "target",
    ["flurLichtschalter", "kuecheButton", "wzPcfingerbot_unknown", "bridge/request/restart", "", "Alle_Lampen/set"],
)
async def test_unknown_or_remote_targets(pub, transport, target):
    assert not await pub.send(target, {"state": "OFF"})
    assert transport.sent == []


@pytest.mark.parametrize("payload", [{}, {"transition": 2}, {"brightness": "hell"}, {"brightness": True}, None])
async def test_invalid_payloads(pub, transport, payload):
    assert not await pub.send("wzBettlampe", payload)
    assert transport.sent == []


# --------------------------------------------------------------------------- dry run and failures


async def test_dry_run_logs_but_does_not_publish(config, transport, caplog):
    pub = Publisher(config, FakeClock(datetime(2026, 11, 12)), base_topic="zigbee2mqtt", dry_run=True, transport=transport)
    seen = []
    pub.add_listener(seen.append)
    with caplog.at_level("INFO"):
        assert await pub.send("flurDeckenstrip", {"state": "ON", "brightness": 50, "color_temp": 200})
    assert transport.sent == []
    assert "DRY RUN" in caplog.text and "flurDeckenstrip" in caplog.text
    assert len(pub.history) == 1 and pub.history[0].dry_run
    assert seen and seen[0].target == "flurDeckenstrip"


async def test_not_connected_returns_false(config):
    pub = Publisher(config, FakeClock(datetime(2026, 11, 12)), base_topic="zigbee2mqtt", dry_run=False)
    assert not await pub.send("flurDeckenstrip", {"state": "OFF"})


async def test_transport_error_does_not_raise(config):
    async def broken(topic, text):
        raise ConnectionError("broker gone")

    pub = Publisher(config, FakeClock(datetime(2026, 11, 12)), base_topic="zigbee2mqtt", dry_run=False, transport=broken)
    assert not await pub.send("flurDeckenstrip", {"state": "OFF"})


async def test_no_lamp_or_group_in_config_can_reach_pveswitch(config, pub, transport):
    """Brute force: every accepted target string is not pveSwitch."""
    for name in list(config.devices) + sorted(config.group_names()):
        await pub.send(name, {"state": "OFF"})
    assert all("pveSwitch" not in topic for topic, _ in transport.sent)
