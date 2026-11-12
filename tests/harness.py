"""In-process test rig: real services, fake clock, fake Z2M echo (no broker needed)."""

from __future__ import annotations

import json
from datetime import datetime

from app.clock import FakeClock
from app.config import AppConfig
from app.mqtt.publisher import Publisher
from app.services.lights import LightService
from app.services.schedule import Schedule
from app.services.scheduler import Scheduler
from app.state import StateStore
from app.store import Store

_CONFIG = None


def config() -> AppConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AppConfig.load()
    return _CONFIG


class EchoZ2M:
    """Transport that applies commands to the state store like Z2M would."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.state: StateStore | None = None
        self.sent: list[tuple[str, dict]] = []
        self.drop: set[str] = set()  # lamps that ignore commands

    async def __call__(self, topic: str, text: str) -> None:
        payload = json.loads(text)
        target = topic.split("/")[1]
        self.sent.append((target, payload))
        if target == "Alle_Lampen":
            members = [d.friendly_name for d in self.cfg.devices.values() if d.in_all_off]
        elif target in self.cfg.devices:
            members = [target]
        else:
            room = next(r.room for r in self.cfg.rooms.values() if r.z2m_group == target)
            members = [d.friendly_name for d in self.cfg.lamps(room)]
        for name in members:
            if name in self.drop:
                continue
            report = {k: v for k, v in payload.items() if k in ("state", "brightness", "color_temp")}
            self.state.handle_message(f"zigbee2mqtt/{name}", json.dumps(report))

    def report(self, name: str, **values) -> None:
        """A lamp reports something by itself (switched outside the app)."""
        self.state.handle_message(f"zigbee2mqtt/{name}", json.dumps(values))

    def clear(self) -> None:
        self.sent.clear()

    def to(self, name: str) -> list[dict]:
        return [p for t, p in self.sent if t == name]


class Rig:
    def __init__(self, start: datetime, *, dry_run: bool = False) -> None:
        self.cfg = config()
        self.clock = FakeClock(start)
        self.store = Store(":memory:")
        self.state = StateStore(self.cfg, self.clock)
        self.z2m = EchoZ2M(self.cfg)
        self.z2m.state = self.state
        self.publisher = Publisher(self.cfg, self.clock, base_topic="zigbee2mqtt", dry_run=dry_run, transport=self.z2m)
        self.schedule = Schedule(self.store, self.clock)
        self.lights = LightService(
            self.cfg, self.state, self.publisher, self.store, self.clock,
            phase=self.schedule.phase, mired=self.schedule.mired,
        )
        self.scheduler = Scheduler(self.schedule, self.lights, self.store, self.clock)

    def all_off_state(self) -> None:
        for d in self.cfg.devices.values():
            if d.on_off and not d.protected:
                self.z2m.report(d.friendly_name, state="OFF")

    def lamp(self, name: str):
        return self.state.get(name)
