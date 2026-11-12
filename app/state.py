"""Live device state, built from what Zigbee2MQTT publishes.

Z2M is the single source of truth for lamp state. This cache only remembers
the last reported values, and tells subscribers what changed. Button presses
arrive as `action` in the device payload; they are passed on as ActionEvents
and never stored as state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Union

from app.clock import Clock
from app.config import AppConfig

log = logging.getLogger(__name__)

TRACKED_KEYS = ("state", "brightness", "color_temp", "power", "energy", "linkquality", "battery")


@dataclass
class DeviceState:
    name: str
    state: str | None = None  # "ON" / "OFF" / None = unknown
    brightness: int | None = None  # 0-254
    color_temp: int | None = None  # mired
    power: float | None = None  # W
    energy: float | None = None  # kWh
    linkquality: int | None = None
    battery: int | None = None
    available: bool | None = None  # from <name>/availability (if Z2M has availability enabled)
    updated_at: float | None = None  # unix timestamp of the last report

    @property
    def is_on(self) -> bool:
        return self.state == "ON"

    def snapshot(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (*TRACKED_KEYS, "available", "updated_at")}


@dataclass(frozen=True)
class StateChanged:
    name: str
    changes: dict[str, tuple[Any, Any]]  # key -> (old, new)


@dataclass(frozen=True)
class ActionEvent:
    name: str
    action: str
    at: float


@dataclass(frozen=True)
class BridgeChanged:
    online: bool | None


Event = Union[StateChanged, ActionEvent, BridgeChanged]


@dataclass
class StateStore:
    config: AppConfig
    clock: Clock
    base_topic: str = "zigbee2mqtt"
    devices: dict[str, DeviceState] = field(default_factory=dict)
    bridge_online: bool | None = None
    z2m_devices: list[dict] | None = None  # bridge/devices (retained)
    z2m_groups: list[dict] | None = None  # bridge/groups (retained)
    last_message_at: float | None = None

    def __post_init__(self) -> None:
        for name in self.config.devices:
            self.devices.setdefault(name, DeviceState(name))
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers.append(callback)

    def _emit(self, event: Event) -> None:
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not stop the others
                log.exception("State subscriber failed for %s", event)

    # ------------------------------------------------------------------ input

    def handle_message(self, topic: str, payload: bytes | str) -> None:
        prefix = self.base_topic + "/"
        if not topic.startswith(prefix):
            return
        sub = topic[len(prefix):]
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
        self.last_message_at = self.clock.now().timestamp()

        if sub == "bridge/state":
            self._handle_bridge_state(text)
        elif sub == "bridge/devices":
            self.z2m_devices = _json(text, list)
        elif sub == "bridge/groups":
            self.z2m_groups = _json(text, list)
        elif sub.startswith("bridge/"):
            return
        elif sub.endswith("/availability"):
            self._handle_availability(sub[: -len("/availability")], text)
        elif "/" in sub:
            return  # <name>/set, <name>/get and other sub-topics (incl. our own commands)
        else:
            self._handle_device(sub, text)

    def _handle_bridge_state(self, text: str) -> None:
        data = _json(text, dict, quiet=True)
        value = (data or {}).get("state") if data is not None else text.strip()
        online = {"online": True, "offline": False}.get(str(value).lower())
        if online != self.bridge_online:
            self.bridge_online = online
            log.info("Zigbee2MQTT is %s", value)
            self._emit(BridgeChanged(online))

    def _handle_availability(self, name: str, text: str) -> None:
        device = self.devices.get(name)
        if device is None:
            return
        data = _json(text, dict, quiet=True)
        value = (data or {}).get("state") if data is not None else text.strip()
        available = {"online": True, "offline": False}.get(str(value).lower())
        if available != device.available:
            old = device.available
            device.available = available
            self._emit(StateChanged(name, {"available": (old, available)}))

    def _handle_device(self, name: str, text: str) -> None:
        device = self.devices.get(name)
        if device is None:
            return  # groups and devices not in the config
        data = _json(text, dict)
        if data is None:
            return
        now = self.clock.now().timestamp()

        action = data.get("action")
        if isinstance(action, str) and action:
            self._emit(ActionEvent(name, action, now))

        # Z2M copies group command values into every member's state, so plugs
        # in Alle_Lampen "report" a color_temp they don't have. Ignore light
        # attributes for devices that can't dim.
        dimmable = self.config.devices[name].dimmable
        changes: dict[str, tuple[Any, Any]] = {}
        for key in TRACKED_KEYS:
            if key not in data or (not dimmable and key in ("brightness", "color_temp")):
                continue
            new = data[key]
            if key == "state" and isinstance(new, str):
                new = new.upper()
            old = getattr(device, key)
            if new != old:
                setattr(device, key, new)
                changes[key] = (old, new)
        device.updated_at = now
        if changes:
            self._emit(StateChanged(name, changes))

    # ------------------------------------------------------------------ queries

    def get(self, name: str) -> DeviceState:
        return self.devices[name]

    def lamps_on(self, room: str | None = None) -> list[str]:
        return [d.friendly_name for d in self.config.lamps(room) if self.devices[d.friendly_name].is_on]


def _json(text: str, kind: type, quiet: bool = False):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        if not quiet:
            log.warning("Ignoring non-JSON payload: %.80s", text)
        return None
    return data if isinstance(data, kind) else None
