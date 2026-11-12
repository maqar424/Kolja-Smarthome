import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.clock import FakeClock
from app.config import AppConfig, Settings


def test_settings_safe_defaults():
    s = Settings.load(env={}, env_file=None)
    assert s.dry_run is True
    assert s.buttons_enabled is False
    assert s.base_topic == "zigbee2mqtt"


def test_settings_from_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text('# comment\nMQTT_HOST="10.0.0.5"\nDRY_RUN=false\nBUTTONS_ENABLED=yes\nDATA_DIR=/tmp/x\n')
    s = Settings.load(env={"MQTT_PORT": "1884"}, env_file=env)
    assert (s.mqtt_host, s.mqtt_port, s.dry_run, s.buttons_enabled) == ("10.0.0.5", 1884, False, True)
    assert s.data_dir == Path("/tmp/x")


def test_environment_overrides_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("DRY_RUN=false\n")
    assert Settings.load(env={"DRY_RUN": "true"}, env_file=env).dry_run is True


def test_app_config_lamps():
    cfg = AppConfig.load()
    wz = [d.friendly_name for d in cfg.lamps("Wohnzimmer")]
    assert wz == ["wzBettlampe", "wzBodenstrips", "wzDeckenlampe", "wzSchreibtischlampe", "wzTowerlampe", "wzLavalampe"]
    assert "pveSwitch" not in [d.friendly_name for d in cfg.lamps()]
    assert cfg.devices["wzTowerlampe"].has_colour is False
    assert cfg.devices["kuecheTischlampe"].has_colour is True
    assert cfg.brightness["night"]["wzBettlampe"].pct == 5.1
    assert cfg.rooms["Wohnzimmer"].display_name == "Schlafzimmer"
    assert {d.friendly_name for d in cfg.power_devices()} == {
        "pveSwitch", "kuecheWasserkocher", "wzTowerlampe", "wzLavalampe"
    }


async def test_fake_clock_wakes_sleepers_in_order():
    clock = FakeClock(datetime(2026, 3, 29, 1, 0))  # DST switch night in Berlin
    woke: list[tuple[str, datetime]] = []

    async def sleeper(name, seconds):
        await clock.sleep(seconds)
        woke.append((name, clock.now()))

    tasks = [asyncio.create_task(sleeper("b", 7200)), asyncio.create_task(sleeper("a", 60))]
    await clock.advance(3 * 3600)
    await asyncio.gather(*tasks)
    assert [n for n, _ in woke] == ["a", "b"]
    # 01:00 CET + 2 h real time = 04:00 CEST (clock skips 02:00-03:00)
    assert woke[1][1].hour == 4
    start = datetime(2026, 3, 29, 1, 0, tzinfo=clock.tz)
    assert clock.now().astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=3)
