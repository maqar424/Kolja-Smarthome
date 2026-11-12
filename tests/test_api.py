"""HTTP API end to end (in-process: TestClient + echo Z2M, no broker)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.clock import LOCAL_TZ, FakeClock
from app.config import Settings
from app.main import build_services, create_app
from app.store import Store
from tests.harness import EchoZ2M, config


@pytest.fixture
def api():
    cfg = config()
    clock = FakeClock(datetime(2026, 11, 12, 17, 0, tzinfo=LOCAL_TZ))
    settings = Settings.load(env={"DRY_RUN": "false"}, env_file=None)
    services = build_services(settings, cfg, clock, with_mqtt=False, store=Store(":memory:"))
    z2m = EchoZ2M(cfg)
    z2m.state = services.state
    services.publisher.set_transport(z2m)
    with TestClient(create_app(services)) as client:
        client.services, client.z2m = services, z2m
        yield client


def test_schedule_overview(api):
    body = api.get("/api/schedule").json()
    assert body["phase"] == "kalt" and body["can_advance"] and body["next_phase"] == "warm"
    assert body["next_boundary"]["phase"] == "warm"
    assert [s["phase"] for s in body["segments"]] == ["night", "kalt", "warm", "twilight", "night"]
    assert body["colour"] == {"master_ct": 0.0, "mired": 153, "ramping": False, "override": False}


def test_next_phase_until_night(api):
    assert api.post("/api/schedule/next").json() == {"phase": "warm"}
    assert api.post("/api/schedule/next").json() == {"phase": "twilight"}
    assert api.post("/api/schedule/next").json() == {"phase": "night"}
    r = api.post("/api/schedule/next")
    assert r.status_code == 409
    body = api.get("/api/schedule").json()
    assert body["phase"] == "night" and not body["can_advance"]
    assert body["next_boundary"]["phase"] == "kalt"


def test_settings_validation(api):
    r = api.put("/api/schedule/settings", json={"warm": "06:00"})
    assert r.status_code == 400 and r.json()["detail"]["errors"]
    r = api.put("/api/schedule/settings", json={"warm": "16:00", "sun_mode": True})
    assert r.status_code == 200 and r.json()["warm"] == "16:00"


def test_colour_override_and_back(api):
    assert api.post("/api/colour", json={"master_ct": 0.5}).json()["mired"] == 262
    assert api.get("/api/schedule").json()["colour"]["override"] is True
    api.delete("/api/colour")
    assert api.get("/api/schedule").json()["colour"]["override"] is False
    assert api.post("/api/colour", json={"master_ct": 2}).status_code == 422


def test_room_actions(api):
    assert api.post("/api/rooms/Küche/on").status_code == 200
    assert api.services.state.get("kuecheDeckenlampe").is_on
    assert api.post("/api/rooms/Küche/dim", json={"direction": "down"}).json() == {"changed": True}
    assert api.post("/api/rooms/Küche/scene", json={"scene": "night"}).status_code == 200
    assert api.services.state.get("kuecheTischlampe").is_on
    assert not api.services.state.get("kuecheDeckenlampe").is_on
    assert api.post("/api/rooms/Küche/off").status_code == 200
    assert api.post("/api/rooms/Keller/on").status_code == 404
    assert api.post("/api/rooms/Küche/scene", json={"scene": "party"}).status_code == 422


def test_lamp_actions(api):
    assert api.post("/api/lamps/flurDeckenstrip", json={"on": True, "brightness": 100}).json() == {"ok": True}
    assert api.services.state.get("flurDeckenstrip").brightness == 100
    assert api.post("/api/lamps/pveSwitch", json={"on": True}).status_code == 404
    assert api.post("/api/lamps/flurDeckenstrip", json={"brightness": 0}).status_code == 422
    api.services.lights.locked = {"wzDeckenlampe"}
    assert api.post("/api/lamps/wzDeckenlampe", json={"on": False}).status_code == 409


def test_all_on_off(api):
    api.post("/api/all-on")
    assert all(api.services.lights.room_is_on(r) for r in api.services.config.rooms)
    api.post("/api/all-off")
    assert ("Alle_Lampen", {"state": "OFF", "transition": 0.4}) in api.z2m.sent
    assert not any(t == "pveSwitch" for t, _ in api.z2m.sent)


def test_state_includes_room_scene_and_lock(api):
    api.post("/api/rooms/Bad/on")
    body = api.get("/api/state").json()
    bad = next(r for r in body["rooms"] if r["room"] == "Bad")
    assert bad["scene"] == "kalt" and bad["factor"] == 1.0
    assert body["devices"]["wzDeckenlampe"]["locked"] is False
