"""Power recording: 1-minute samples, gaps, retention, bucketed history."""

import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.clock import LOCAL_TZ, FakeClock
from app.config import Settings
from app.main import build_services, create_app
from app.services.power import PowerRecorder
from app.state import StateStore
from app.store import Store
from tests.harness import config

START = datetime(2026, 11, 13, 10, 0, 20, tzinfo=LOCAL_TZ)


@pytest.fixture
def rig():
    cfg = config()
    clock = FakeClock(START)
    state = StateStore(cfg, clock)
    store = Store(":memory:")
    state.handle_message("zigbee2mqtt/bridge/state", '{"state": "online"}')
    return cfg, clock, state, store, PowerRecorder(cfg, state, store, clock)


def report(state, name, **values):
    state.handle_message(f"zigbee2mqtt/{name}", json.dumps(values))


def test_records_all_metering_plugs_including_pveswitch(rig):
    cfg, clock, state, store, rec = rig
    assert set(rec.devices) == {"pveSwitch", "kuecheWasserkocher", "wzTowerlampe", "wzLavalampe"}
    report(state, "pveSwitch", power=31.5)
    report(state, "kuecheWasserkocher", power=0)
    assert rec.sample() == {"pveSwitch": 31.5, "kuecheWasserkocher": 0.0}  # the others never reported


def test_no_samples_while_z2m_offline_or_plug_unavailable(rig):
    _, _, state, _, rec = rig
    report(state, "pveSwitch", power=30)
    report(state, "wzTowerlampe", power=5)
    state.handle_message("zigbee2mqtt/wzTowerlampe/availability", '{"state": "offline"}')
    assert rec.sample() == {"pveSwitch": 30.0}
    state.handle_message("zigbee2mqtt/bridge/state", '{"state": "offline"}')
    assert rec.sample() == {}


async def test_samples_on_the_full_minute(rig):
    _, clock, state, store, rec = rig
    report(state, "pveSwitch", power=30)
    task = asyncio.create_task(rec.run())
    await clock.advance(39)  # 10:00:59 -> nothing yet
    assert store.power_sum("pveSwitch", 0, 2**40) == (0.0, 0)
    await clock.advance(1)  # 10:01:00
    report(state, "pveSwitch", power=40)
    await clock.advance(60)  # 10:02:00
    task.cancel()
    rows = store.power_buckets("pveSwitch", 0, 2**40, 60)
    t1 = int(datetime(2026, 11, 13, 10, 1, tzinfo=LOCAL_TZ).timestamp())
    assert [(r[0], r[1]) for r in rows] == [(t1, 30.0), (t1 + 60, 40.0)]


def test_history_buckets_average_peak_and_energy(rig):
    _, clock, _, store, rec = rig
    end = int(clock.now().timestamp()) // 60 * 60
    # one hour at 30 W, plus a 3-minute kettle-like peak of 2000 W
    for i in range(60):
        store.add_power_samples(end - 3600 + i * 60, {"pveSwitch": 2000.0 if 20 <= i < 23 else 30.0})
    h = rec.history("pveSwitch", "24h")
    assert h["bucket_s"] == 300 and h["samples"] == 60
    assert len(h["points"]) in (12, 13)  # 5-minute buckets, aligned to the clock
    assert max(p[2] for p in h["points"]) == 2000.0
    assert h["peak_w"] == 2000.0
    assert h["energy_kwh"] == round((57 * 30 + 3 * 2000) / 60 / 1000, 3)
    assert h["avg_w"] == round((57 * 30 + 3 * 2000) / 60, 1)


def test_day_buckets_start_at_local_midnight(rig):
    _, clock, _, store, rec = rig
    midnight = int(datetime(2026, 11, 12, 0, 0, tzinfo=LOCAL_TZ).timestamp())
    store.add_power_samples(midnight - 60, {"pveSwitch": 10.0})  # 23:59 the day before
    store.add_power_samples(midnight, {"pveSwitch": 20.0})
    store.add_power_samples(midnight + 3600, {"pveSwitch": 40.0})
    h = rec.history("pveSwitch", "1y")
    assert [p[:2] for p in h["points"]] == [[midnight - 86400, 10.0], [midnight, 30.0]]


def test_empty_history(rig):
    rec = rig[4]
    h = rec.history("wzLavalampe", "6h")
    assert h["points"] == [] and h["samples"] == 0 and h["avg_w"] is None and h["peak_w"] is None


def test_retention_one_year_pruned_once_a_day(rig):
    _, clock, _, store, rec = rig
    now = int(clock.now().timestamp())
    store.add_power_samples(now - 366 * 86400, {"pveSwitch": 1.0})
    store.add_power_samples(now - 364 * 86400, {"pveSwitch": 2.0})
    rec.prune_daily()
    assert store.power_sum("pveSwitch", 0, 2**40) == (2.0, 1)
    store.add_power_samples(now - 400 * 86400, {"pveSwitch": 5.0})
    rec.prune_daily()  # same day: not again
    assert store.power_sum("pveSwitch", 0, 2**40)[1] == 2


def test_samples_survive_reopen(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_power_samples(1000, {"pveSwitch": 12.5})
    store.add_power_samples(1000, {"pveSwitch": 13.0})  # same minute twice: replaced, not duplicated
    store.close()
    assert Store(tmp_path / "db.sqlite").power_sum("pveSwitch", 0, 2000) == (13.0, 1)


def test_api_power_history():
    cfg = config()
    clock = FakeClock(START)
    settings = Settings.load(env={"DRY_RUN": "true"}, env_file=None)
    services = build_services(settings, cfg, clock, with_mqtt=False, store=Store(":memory:"))
    services.store.add_power_samples(int(clock.now().timestamp()) - 120, {"kuecheWasserkocher": 1800.0})
    with TestClient(create_app(services)) as client:
        body = client.get("/api/power/kuecheWasserkocher?range=6h").json()
        assert body["points"][0][1] == 1800.0 and body["range"] == "6h"
        assert client.get("/api/power/kuecheWasserkocher").json()["range"] == "24h"
        assert client.get("/api/power/kuecheWasserkocher?range=2h").status_code == 400
        assert client.get("/api/power/wzBettlampe").status_code == 404  # a lamp without metering
        assert client.get("/api/power/nope").status_code == 404
