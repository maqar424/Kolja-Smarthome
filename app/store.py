"""Persistent state in SQLite (survives restarts and redeploys via the data volume).

A small key/value table with JSON values is enough for settings and runtime
state. Writes are tiny and rare, so plain synchronous sqlite3 is fine.

Stored:
  schedule          ScheduleSettings (phase times, sun mode, ramps, location)
  manual_advances   "next phase" presses of the last days
  colour_override   master slider value + time, or null
  room/<room>       RoomLight: active scene, dim factor, base brightness per lamp
  applied_phase     the phase the lamps were last switched to (detects a
                    boundary missed while the app was down)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.colour import ColourEvent
from app.domain.phases import ManualAdvance
from app.domain.scenes import RoomLight
from app.domain.settings import ScheduleSettings


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ raw

    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
        )

    def delete(self, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ------------------------------------------------------------------ typed

    def schedule(self) -> ScheduleSettings:
        return ScheduleSettings.from_dict(self.get("schedule", {}))

    def save_schedule(self, settings: ScheduleSettings) -> None:
        self.set("schedule", settings.to_dict())

    def manual_advances(self) -> list[ManualAdvance]:
        return [ManualAdvance(datetime.fromisoformat(m["at"]), m["to"]) for m in self.get("manual_advances", [])]

    def add_manual_advance(self, advance: ManualAdvance) -> None:
        keep_after = advance.at - timedelta(days=3)
        items = [m for m in self.manual_advances() if m.at >= keep_after] + [advance]
        self.set("manual_advances", [{"at": m.at.isoformat(), "to": m.to} for m in items])

    def colour_override(self) -> ColourEvent | None:
        data = self.get("colour_override")
        if not data:
            return None
        return ColourEvent(datetime.fromisoformat(data["at"]), "override", float(data["value"]))

    def set_colour_override(self, at: datetime, value: float) -> None:
        # Only the latest slider value matters: it holds until the next ramp.
        self.set("colour_override", {"at": at.isoformat(), "value": value})

    def clear_colour_override(self) -> None:
        self.delete("colour_override")

    def room_light(self, room: str) -> RoomLight:
        return RoomLight.from_dict(self.get(f"room/{room}"))

    def save_room_light(self, room: str, light: RoomLight) -> None:
        self.set(f"room/{room}", light.to_dict())

    def applied_phase(self) -> dict | None:
        """{"phase": str, "at": iso boundary time} or None on first start."""
        return self.get("applied_phase")

    def set_applied_phase(self, phase: str, boundary_at: datetime) -> None:
        self.set("applied_phase", {"phase": phase, "at": boundary_at.isoformat()})
