import copy
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.config import AppConfig, Settings
from app.consistency import check_consistency
from app.main import build_services, create_app
from app.mqtt.publisher import Publisher
from app.state import ActionEvent, BridgeChanged, StateChanged, StateStore
from app.store import Store
from app.sysclock import clock_synchronised


@pytest.fixture(scope="module")
def config():
    return AppConfig.load()


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 11, 12, 20, 0))


@pytest.fixture
def store(config, clock):
    return StateStore(config, clock)


def msg(store, sub, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    store.handle_message(f"zigbee2mqtt/{sub}", text.encode())


# --------------------------------------------------------------------------- state cache


def test_device_state_updates_and_emits_changes(store):
    events = []
    store.subscribe(events.append)
    msg(store, "wzBettlampe", {"state": "ON", "brightness": 120, "color_temp": 300, "linkquality": 80})
    msg(store, "wzBettlampe", {"state": "ON", "brightness": 120, "color_temp": 300, "linkquality": 80})
    msg(store, "wzBettlampe", {"state": "OFF"})
    dev = store.get("wzBettlampe")
    assert (dev.state, dev.brightness, dev.color_temp) == ("OFF", 120, 300)
    changes = [e for e in events if isinstance(e, StateChanged)]
    assert len(changes) == 2  # the identical repeat emits nothing
    assert changes[1].changes == {"state": ("ON", "OFF")}


def test_action_is_event_not_state(store):
    events = []
    store.subscribe(events.append)
    msg(store, "flurLichtschalter", {"action": "on_press", "battery": 100})
    msg(store, "flurLichtschalter", {"action": "", "battery": 100})  # Z2M may clear the action
    actions = [e for e in events if isinstance(e, ActionEvent)]
    assert [(a.name, a.action) for a in actions] == [("flurLichtschalter", "on_press")]


def test_plugs_ignore_group_copied_light_values(store):
    """Seen live: plugs in Alle_Lampen report color_temp copied from group commands."""
    msg(store, "wzTowerlampe", {"state": "ON", "color_temp": 309, "power": 7})
    dev = store.get("wzTowerlampe")
    assert (dev.state, dev.color_temp, dev.brightness, dev.power) == ("ON", None, None, 7)


def test_power_tracked(store):
    msg(store, "pveSwitch", {"state": "ON", "power": 41.5, "energy": 120.3})
    assert store.get("pveSwitch").power == 41.5


def test_bridge_state_json_and_plain(store):
    events = []
    store.subscribe(events.append)
    msg(store, "bridge/state", {"state": "online"})
    assert store.bridge_online is True
    msg(store, "bridge/state", "offline")  # older Z2M versions publish plain text
    assert store.bridge_online is False
    assert [e.online for e in events if isinstance(e, BridgeChanged)] == [True, False]


def test_ignored_topics(store):
    msg(store, "wzBettlampe/set", {"state": "ON"})  # our own command echoed back
    msg(store, "Wohnzimmer", {"state": "ON"})  # group state
    msg(store, "unknownDevice", {"state": "ON"})
    msg(store, "wzBettlampe", "not json")
    store.handle_message("other/wzBettlampe", b'{"state":"ON"}')
    assert store.get("wzBettlampe").state is None


def test_availability(store):
    msg(store, "badDeckenlampe/availability", {"state": "offline"})
    assert store.get("badDeckenlampe").available is False


def test_lamps_on(store):
    msg(store, "kuecheDeckenlampe", {"state": "ON"})
    msg(store, "pveSwitch", {"state": "ON"})
    assert store.lamps_on("Küche") == ["kuecheDeckenlampe"]
    assert "pveSwitch" not in store.lamps_on()


def test_bridge_devices_retained_list(store, z2m_snapshot):
    msg(store, "bridge/devices", z2m_snapshot["devices"])
    assert len(store.z2m_devices) == 26


# --------------------------------------------------------------------------- consistency


def test_consistency_clean_on_snapshot(config, z2m_snapshot):
    assert check_consistency(config, z2m_snapshot["devices"], z2m_snapshot["groups"]) == []


def test_consistency_live_format_with_definition(config, z2m_snapshot):
    """Live bridge/devices nests exposes under `definition`."""
    live = [{**d, "definition": {"exposes": d["exposes"]}} for d in z2m_snapshot["devices"]]
    for d in live:
        del d["exposes"]
    assert check_consistency(config, live, z2m_snapshot["groups"]) == []


def test_consistency_live_groups_by_ieee_address(config, z2m_snapshot):
    """Live bridge/groups has members as {ieee_address, endpoint} only (found on the real Pi)."""
    ieee = {d["friendly_name"]: d["ieee_address"] for d in z2m_snapshot["devices"]}
    groups = copy.deepcopy(z2m_snapshot["groups"])
    for g in groups:
        g["members"] = [{"ieee_address": ieee[m["friendly_name"]], "endpoint": m["endpoint"]} for m in g["members"]]
    assert check_consistency(config, z2m_snapshot["devices"], groups) == []
    groups[-1]["members"].append({"ieee_address": ieee["pveSwitch"], "endpoint": 1})
    assert any("PROTECTED" in w and "pveSwitch" in w for w in check_consistency(config, z2m_snapshot["devices"], groups))


def test_consistency_reports_problems(config, z2m_snapshot):
    devices = [d for d in z2m_snapshot["devices"] if d["friendly_name"] != "flurDeckenstrip"]
    groups = copy.deepcopy(z2m_snapshot["groups"])
    alle = next(g for g in groups if g["friendly_name"] == "Alle_Lampen")
    alle["members"].append({"friendly_name": "pveSwitch", "endpoint": 1})
    warnings = check_consistency(config, devices, groups)
    assert any("flurDeckenstrip" in w for w in warnings)
    assert any("PROTECTED" in w and "pveSwitch" in w for w in warnings)


# --------------------------------------------------------------------------- state reads


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, topic, text):
        self.sent.append((topic, text))


async def test_state_read_never_for_pveswitch_or_remotes(config, clock):
    rec = Recorder()
    pub = Publisher(config, clock, base_topic="zigbee2mqtt", dry_run=False, transport=rec)
    assert await pub.request_state("wzBettlampe")
    assert not await pub.request_state("pveSwitch")
    assert not await pub.request_state("flurLichtschalter")
    assert not await pub.request_state("Alle_Lampen")
    assert rec.sent == [("zigbee2mqtt/wzBettlampe/get", '{"state":""}')]


async def test_state_read_in_dry_run_needs_opt_in(config, clock):
    rec = Recorder()
    pub = Publisher(config, clock, base_topic="zigbee2mqtt", dry_run=True, transport=rec)
    await pub.request_state("wzBettlampe")
    assert rec.sent == []
    pub.allow_reads_in_dry_run = True
    await pub.request_state("wzBettlampe")
    assert len(rec.sent) == 1


# --------------------------------------------------------------------------- HTTP


@pytest.fixture
def client(config, clock):
    services = build_services(Settings.load(env={}, env_file=None), config, clock, with_mqtt=False, store=Store(":memory:"))
    with TestClient(create_app(services)) as c:
        c.services = services
        yield c


def test_health_degraded_without_mqtt(client):
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["dry_run"] is True and body["buttons_enabled"] is False


def test_api_state_lists_rooms_and_lamps(client):
    msg(client.services.state, "kuecheTwintowers", {"state": "ON", "brightness": 200})
    body = client.get("/api/state").json()
    rooms = {r["room"]: r for r in body["rooms"]}
    assert rooms["Wohnzimmer"]["display_name"] == "Schlafzimmer"
    assert rooms["Küche"]["on"] is True and rooms["Bad"]["on"] is False
    assert body["devices"]["kuecheTwintowers"]["brightness"] == 200
    assert "pveSwitch" in body["devices"]  # power only
    assert "flurLichtschalter" not in body["devices"]


def test_clock_sync_check_runs():
    assert clock_synchronised() in (True, False, None)
