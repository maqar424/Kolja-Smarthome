"""End-to-end over a real (local, throwaway) MQTT broker with a fake Zigbee2MQTT.

Start the broker first:  docker compose -f docker-compose.test.yml up -d
Without it these tests are skipped. Never point them at the Pi.
"""

import asyncio
import os
import socket
import uuid
from datetime import datetime

import pytest

from app.clock import FakeClock
from app.config import AppConfig, Settings
from app.main import build_services
from tests.fake_z2m import FakeZ2M

HOST = os.environ.get("TEST_MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("TEST_MQTT_PORT", "18830"))


def _broker_up() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _broker_up(), reason=f"no test broker on {HOST}:{PORT}")


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
def base_topic():
    return f"z2mtest-{uuid.uuid4().hex[:8]}"  # isolated per test


def make_services(base_topic, *, dry_run):
    settings = Settings.load(
        env={"MQTT_HOST": HOST, "MQTT_PORT": str(PORT), "Z2M_BASE_TOPIC": base_topic, "DRY_RUN": str(dry_run)},
        env_file=None,
    )
    return build_services(settings, AppConfig.load(), FakeClock(datetime(2026, 11, 12, 20, 0)))


async def run_app(services):
    task = asyncio.create_task(services.mqtt.run())
    await wait_for(lambda: services.mqtt.connected and services.mqtt.consistency_warnings is not None)
    return task


async def stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_startup_reads_states_and_checks_config(z2m_snapshot, base_topic):
    async with FakeZ2M(z2m_snapshot, HOST, PORT, base_topic) as z2m:
        z2m.set_state("wzBettlampe", state="ON", brightness=100, color_temp=300)
        services = make_services(base_topic, dry_run=False)
        task = await run_app(services)
        try:
            assert services.state.bridge_online is True
            assert services.mqtt.consistency_warnings == []
            await wait_for(lambda: services.state.get("wzBettlampe").brightness == 100)
            gets = [t for t, _ in z2m.received if t.endswith("/get")]
            assert "pveSwitch/get" not in gets
            assert "wzBettlampe/get" in gets
        finally:
            await stop(task)


async def test_command_round_trip(z2m_snapshot, base_topic):
    async with FakeZ2M(z2m_snapshot, HOST, PORT, base_topic) as z2m:
        services = make_services(base_topic, dry_run=False)
        task = await run_app(services)
        try:
            assert await services.publisher.send("flurDeckenstrip", {"state": "ON", "brightness": 80, "color_temp": 250})
            await wait_for(lambda: services.state.get("flurDeckenstrip").brightness == 80)
            assert services.state.get("flurDeckenstrip").color_temp == 250

            assert await services.publisher.send("Alle_Lampen", {"state": "OFF"})
            await wait_for(lambda: services.state.get("flurDeckenstrip").state == "OFF")
        finally:
            await stop(task)


async def test_dry_run_sends_nothing(z2m_snapshot, base_topic):
    async with FakeZ2M(z2m_snapshot, HOST, PORT, base_topic) as z2m:
        services = make_services(base_topic, dry_run=True)
        task = await run_app(services)
        try:
            assert await services.publisher.send("flurDeckenstrip", {"state": "ON"})
            await asyncio.sleep(0.5)
            assert z2m.received == []  # not even the state reads (opt-in only)
        finally:
            await stop(task)


async def test_button_press_arrives_as_action(z2m_snapshot, base_topic):
    from app.state import ActionEvent

    async with FakeZ2M(z2m_snapshot, HOST, PORT, base_topic) as z2m:
        services = make_services(base_topic, dry_run=True)
        actions = []
        services.state.subscribe(lambda e: actions.append(e) if isinstance(e, ActionEvent) else None)
        task = await run_app(services)
        try:
            await z2m.press("flurLichtschalter", "on_press")
            await wait_for(lambda: actions)
            assert (actions[0].name, actions[0].action) == ("flurLichtschalter", "on_press")
        finally:
            await stop(task)
