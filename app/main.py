"""FastAPI application: wires config, clock, state, publisher and MQTT together.

Run locally:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.clock import Clock, RealClock
from app.config import AppConfig, Settings
from app.mqtt.client import MqttClient
from app.mqtt.publisher import Publisher
from app.state import StateStore
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


def build_services(settings: Settings, config: AppConfig, clock: Clock, *, with_mqtt: bool = True) -> Services:
    state = StateStore(config, clock, base_topic=settings.base_topic)
    publisher = Publisher(
        config,
        clock,
        base_topic=settings.base_topic,
        dry_run=settings.dry_run,
        allow_reads_in_dry_run=settings.dry_run_allow_reads,
    )
    mqtt = MqttClient(settings, config, state, publisher) if with_mqtt else None
    return Services(settings, config, clock, state, publisher, mqtt)


def create_app(services: Services) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = services.settings
        log.info("Starting - dry run: %s, buttons enabled: %s, broker: %s:%s",
                 s.dry_run, s.buttons_enabled, s.mqtt_host, s.mqtt_port)
        task = asyncio.create_task(services.mqtt.run(), name="mqtt") if services.mqtt else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Kolja-Smarthome", lifespan=lifespan)
    app.state.services = services

    @app.get("/health")
    async def health():
        mqtt_ok = services.mqtt.connected if services.mqtt else False
        z2m_ok = services.state.bridge_online is True
        synced = clock_synchronised()
        body = {
            "status": "ok" if (mqtt_ok and z2m_ok and synced is not False) else "degraded",
            "mqtt_connected": mqtt_ok,
            "z2m_online": z2m_ok,
            "clock_synchronised": synced,
            "dry_run": services.settings.dry_run,
            "buttons_enabled": services.settings.buttons_enabled,
            "refused_commands": services.publisher.refused_count,
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


def _default_app() -> FastAPI:
    settings = Settings.load()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return create_app(build_services(settings, AppConfig.load(), RealClock()))


app = _default_app()
