"""FastAPI application: wires config, clock, state, publisher and MQTT together.

Run locally:  uvicorn app.main:create_default_app --factory --port 8000

(A factory instead of a module-level `app` so that importing this module,
e.g. in tests, does not open the database or read .env.)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import build_router
from app.clock import Clock, RealClock
from app.config import AppConfig, Settings
from app.mqtt.client import MqttClient
from app.mqtt.publisher import Publisher
from app.services.buttons import ButtonService
from app.services.lights import LightService
from app.services.schedule import Schedule
from app.services.scheduler import Scheduler
from app.services.sunrise import SunriseService
from app.state import StateStore
from app.store import Store
from app.sysclock import clock_synchronised

log = logging.getLogger("app")


@dataclass
class Services:
    settings: Settings
    config: AppConfig
    clock: Clock
    state: StateStore
    publisher: Publisher
    mqtt: MqttClient | None
    store: Store
    schedule: Schedule
    lights: LightService
    scheduler: Scheduler
    buttons: ButtonService
    sunrise: SunriseService


def build_services(
    settings: Settings,
    config: AppConfig,
    clock: Clock,
    *,
    with_mqtt: bool = True,
    store: Store | None = None,
) -> Services:
    store = store or Store(settings.data_dir / "smarthome.db")
    state = StateStore(config, clock, base_topic=settings.base_topic)
    publisher = Publisher(
        config,
        clock,
        base_topic=settings.base_topic,
        dry_run=settings.dry_run,
        allow_reads_in_dry_run=settings.dry_run_allow_reads,
    )
    mqtt = MqttClient(settings, config, state, publisher) if with_mqtt else None
    # Schedule needs the sunrise's early day starts and the sunrise needs the
    # lights, which need the schedule: break the cycle with a late-bound lambda.
    holder: dict = {}
    schedule = Schedule(store, clock, wakes=lambda: holder["sunrise"].early_starts())
    lights = LightService(config, state, publisher, store, clock, phase=schedule.phase, mired=schedule.mired)
    scheduler = Scheduler(schedule, lights, store, clock)
    sunrise = SunriseService(
        config, state, lights, publisher, store, clock,
        connected=lambda: publisher.dry_run or (mqtt is not None and mqtt.connected),
        clock_synced=clock_synchronised,
    )
    holder["sunrise"] = sunrise
    sunrise.on_schedule_change = schedule.changed.set
    sunrise.on_final_applied = scheduler.mark_current_phase_applied
    buttons = ButtonService(
        config, state, lights, publisher, store, clock,
        enabled=settings.buttons_enabled, room_blocked=sunrise.room_blocked,
    )
    return Services(
        settings, config, clock, state, publisher, mqtt, store, schedule, lights, scheduler, buttons, sunrise
    )


def create_app(services: Services) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = services.settings
        log.info("Starting - dry run: %s, buttons enabled: %s, broker: %s:%s",
                 s.dry_run, s.buttons_enabled, s.mqtt_host, s.mqtt_port)
        tasks = []
        if services.mqtt:
            tasks.append(asyncio.create_task(services.mqtt.run(), name="mqtt"))
            tasks.append(asyncio.create_task(_start_scheduler(services), name="scheduler"))
        services.buttons.resume()  # finish a kettle timer that ran before a restart
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Kolja-Smarthome", lifespan=lifespan)
    app.state.services = services
    app.include_router(build_router(services))

    @app.get("/health")
    async def health():
        mqtt_ok = services.mqtt.connected if services.mqtt else False
        z2m_ok = services.state.bridge_online is True
        synced = clock_synchronised()
        beat = services.scheduler.heartbeat
        scheduler_ok = beat is not None and services.clock.now().timestamp() - beat.timestamp() < 120  # timestamps: DST-safe
        body = {
            "status": "ok" if (mqtt_ok and z2m_ok and synced is not False and scheduler_ok) else "degraded",
            "scheduler_alive": scheduler_ok,
            "mqtt_connected": mqtt_ok,
            "z2m_online": z2m_ok,
            "clock_synchronised": synced,
            "dry_run": services.settings.dry_run,
            "buttons_enabled": services.settings.buttons_enabled,
            "refused_commands": services.publisher.refused_count,
            "sunrise_warning": services.sunrise.warning() is not None,
            "config_warnings": services.mqtt.consistency_warnings if services.mqtt else None,
            "time": services.clock.now().isoformat(timespec="seconds"),
        }
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    @app.get("/api/state")
    async def api_state():
        cfg = services.config
        return {
            "rooms": [
                {
                    "room": r.room,
                    "display_name": r.display_name,
                    "svg_id": r.svg_id,
                    "lamps": [d.friendly_name for d in cfg.lamps(r.room)],
                    "on": bool(services.state.lamps_on(r.room)),
                    **services.store.room_light(r.room).to_dict(),
                }
                for r in cfg.rooms.values()
            ],
            "devices": {
                d.friendly_name: {
                    "display_name": d.display_name,
                    "room": d.room,
                    "dimmable": d.dimmable,
                    "has_colour": d.has_colour,
                    "has_power": d.has_power,
                    "locked": d.friendly_name in services.lights.locked,
                    **services.state.get(d.friendly_name).snapshot(),
                }
                for d in cfg.devices.values()
                if d.is_lamp or d.has_power
            },
        }

    @app.get("/api/debug/commands")
    async def recent_commands():
        """Last commands sent (or logged in dry run), newest first."""
        return [
            {"at": c.at, "topic": c.topic, "payload": c.payload, "dry_run": c.dry_run}
            for c in reversed(services.publisher.history)
        ]

    return app


async def _start_scheduler(services: Services) -> None:
    """Start the automation once lamp states are known, so the first tick sees reality."""
    for _ in range(100):  # up to ~10 s
        if services.mqtt and services.mqtt.connected and services.mqtt.consistency_warnings is not None:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(2.0)  # give the state reads a moment to come back
    # The sunrise runs as its own task: nothing in the scheduler can block it.
    sunrise = asyncio.create_task(services.sunrise.run(), name="sunrise")
    try:
        await services.scheduler.run()
    finally:
        sunrise.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sunrise


def create_default_app() -> FastAPI:
    settings = Settings.load()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return create_app(build_services(settings, AppConfig.load(), RealClock()))

