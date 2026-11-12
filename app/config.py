"""Runtime settings (environment / .env) and static config (config/*.json).

Settings come from environment variables. For local development a `.env` file
in the repo root is read as well (values already in the environment win), so
no extra dependency like python-dotenv is needed. In the container, docker
compose passes the same file via `env_file`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

ALL_LAMPS_GROUP = "Alle_Lampen"
COMMON_MIRED = (153, 370)


# --------------------------------------------------------------------------- settings


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    base_topic: str = "zigbee2mqtt"
    # Safe defaults: nothing is published and buttons are ignored unless
    # explicitly enabled (hard rules 3 and 4).
    dry_run: bool = True
    # In dry run, also skip the read-only `<lamp>/get` state requests at startup
    # unless this is set. Without them, lamp states appear only when a lamp reports.
    dry_run_allow_reads: bool = False
    buttons_enabled: bool = False
    http_port: int = 8000
    data_dir: Path = REPO_ROOT / "data"
    log_level: str = "INFO"

    @classmethod
    def load(cls, env: dict[str, str] | None = None, env_file: Path | None = REPO_ROOT / ".env") -> "Settings":
        merged = _read_env_file(env_file) if env_file else {}
        merged.update(os.environ if env is None else env)
        data_dir = Path(merged.get("DATA_DIR") or REPO_ROOT / "data")
        if not data_dir.is_absolute():
            data_dir = REPO_ROOT / data_dir
        return cls(
            mqtt_host=merged.get("MQTT_HOST") or "localhost",
            mqtt_port=int(merged.get("MQTT_PORT") or 1883),
            base_topic=(merged.get("Z2M_BASE_TOPIC") or "zigbee2mqtt").rstrip("/"),
            dry_run=_bool(merged.get("DRY_RUN"), True),
            dry_run_allow_reads=_bool(merged.get("DRY_RUN_ALLOW_READS"), False),
            buttons_enabled=_bool(merged.get("BUTTONS_ENABLED"), False),
            http_port=int(merged.get("HTTP_PORT") or 8000),
            data_dir=data_dir,
            log_level=(merged.get("LOG_LEVEL") or "INFO").upper(),
        )


# --------------------------------------------------------------------------- static config


@dataclass(frozen=True)
class Device:
    friendly_name: str
    room: str | None
    display_name: str
    category: str  # light | appliance | remote
    in_app: bool
    in_room_off: bool
    in_all_off: bool
    sort_order: int
    protected: bool
    model: str | None
    shape: str | None
    geometry: Any
    mount: str | None
    height_cm: list[int] | None
    on_off: bool
    dimmable: bool
    color_temp_range: tuple[int, int] | None
    has_power: bool
    actions: tuple[str, ...]

    @property
    def is_lamp(self) -> bool:
        """A light the app controls (dimmable lamp or lamp on a plug)."""
        return self.category == "light" and self.in_app and not self.protected

    @property
    def has_colour(self) -> bool:
        return self.color_temp_range is not None and self.dimmable


@dataclass(frozen=True)
class Room:
    room: str
    display_name: str
    sort_order: int
    z2m_group: str
    z2m_group_id: int
    svg_id: str


@dataclass(frozen=True)
class SceneValue:
    on: bool
    pct: float | None  # None for on/off-only lamps or when off


@dataclass(frozen=True)
class Scene:
    id: int
    key: str
    display_name: str
    show_in_app: bool
    sort_order: int


@dataclass
class AppConfig:
    devices: dict[str, Device]
    rooms: dict[str, Room]
    scenes: dict[str, Scene]
    # brightness[scene_key][friendly_name] -> SceneValue
    brightness: dict[str, dict[str, SceneValue]] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR) -> "AppConfig":
        def read(name: str) -> dict:
            return json.loads((config_dir / name).read_text(encoding="utf-8"))

        devices = {}
        for d in read("devices.json")["devices"]:
            caps = d.get("capabilities") or {}
            ct = caps.get("color_temp")
            devices[d["friendly_name"]] = Device(
                friendly_name=d["friendly_name"],
                room=d["room"],
                display_name=d["display_name"],
                category=d["category"],
                in_app=d["in_app"],
                in_room_off=d["in_room_off"],
                in_all_off=d["in_all_off"],
                sort_order=d["sort_order"],
                protected=d["protected"],
                model=d.get("model"),
                shape=d.get("shape"),
                geometry=d.get("geometry"),
                mount=d.get("mount"),
                height_cm=d.get("height_cm"),
                on_off=bool(caps.get("on_off")),
                dimmable=bool(caps.get("brightness")),
                color_temp_range=tuple(ct) if ct else None,
                has_power=bool(caps.get("power")),
                actions=tuple(caps.get("actions") or ()),
            )
        rooms = {r["room"]: Room(**r) for r in read("rooms.json")["rooms"]}
        scenes_file = read("scenes.json")
        scenes = {s["key"]: Scene(**s) for s in scenes_file["scenes"]}
        brightness: dict[str, dict[str, SceneValue]] = {}
        for name, per_scene in scenes_file["brightness"].items():
            for key, value in per_scene.items():
                brightness.setdefault(key, {})[name] = SceneValue(on=value["on"], pct=value["pct"])
        return cls(devices=devices, rooms=rooms, scenes=scenes, brightness=brightness)

    # ---- convenience lookups

    def lamps(self, room: str | None = None) -> list[Device]:
        """Lamps the app controls, optionally only those of one room, in display order."""
        return [d for d in self.devices.values() if d.is_lamp and (room is None or d.room == room)]

    def group_names(self) -> set[str]:
        return {r.z2m_group for r in self.rooms.values()} | {ALL_LAMPS_GROUP}

    def power_devices(self) -> list[Device]:
        return [d for d in self.devices.values() if d.has_power]
