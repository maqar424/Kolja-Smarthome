"""User-editable schedule settings (Settings tab), with defaults from the spec."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import time
from typing import Any

PHASES = ("kalt", "warm", "twilight", "night")


@dataclass(frozen=True)
class SunRule:
    """Phase start = sunset + offset, limited to [earliest, latest]."""

    offset_min: int
    earliest: time
    latest: time


@dataclass(frozen=True)
class ScheduleSettings:
    # Fixed start times (manual mode). kalt and night also apply in sun mode.
    kalt: time = time(7, 0)
    warm: time = time(18, 0)
    twilight: time = time(21, 0)
    night: time = time(22, 30)
    # "Nach Sonnenstand"
    sun_mode: bool = False
    latitude: float = 50.94  # Köln
    longitude: float = 6.96
    warm_sun: SunRule = field(default_factory=lambda: SunRule(0, time(18, 0), time(21, 0)))
    twilight_sun: SunRule = field(default_factory=lambda: SunRule(60, time(21, 0), time(22, 0)))
    # Colour ramps (minutes)
    warm_ramp_min: int = 60
    kalt_ramp_min: int = 30

    def validate(self) -> list[str]:
        """Problems that make the settings unusable (shown to the user, not saved)."""
        errors = []
        times = [self.kalt, self.warm, self.twilight, self.night]
        if not all(a < b for a, b in zip(times, times[1:])):
            errors.append("Die Phasen müssen in der Reihenfolge kalt < warm < twilight < night beginnen.")
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            errors.append("Ungültiger Standort.")
        for name, minutes in (("Warm-Übergang", self.warm_ramp_min), ("Kalt-Übergang", self.kalt_ramp_min)):
            if not 0 <= minutes <= 240:
                errors.append(f"{name} muss zwischen 0 und 240 Minuten liegen.")
        for rule in (self.warm_sun, self.twilight_sun):
            if rule.earliest > rule.latest:
                errors.append("Sonnenregel: frühester Zeitpunkt liegt nach dem spätesten.")
        return errors

    # ---- JSON (de)serialisation for the store / API

    def to_dict(self) -> dict[str, Any]:
        def conv(value):
            if isinstance(value, time):
                return value.strftime("%H:%M")
            if isinstance(value, dict):
                return {k: conv(v) for k, v in value.items()}
            return value

        return conv(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleSettings":
        base = cls()
        changes: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if not hasattr(base, key):
                continue
            current = getattr(base, key)
            if isinstance(current, time):
                changes[key] = _parse_time(value)
            elif isinstance(current, SunRule):
                rule = value or {}
                changes[key] = SunRule(
                    int(rule.get("offset_min", current.offset_min)),
                    _parse_time(rule.get("earliest", current.earliest)),
                    _parse_time(rule.get("latest", current.latest)),
                )
            elif isinstance(current, bool):
                changes[key] = bool(value)
            elif isinstance(current, int):
                changes[key] = int(value)
            elif isinstance(current, float):
                changes[key] = float(value)
        return replace(base, **changes)


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    hours, minutes = str(value).strip().split(":")[:2]
    return time(int(hours), int(minutes))
