"""Sunrise wake-up: timeline maths (requirements §7a). Pure functions.

Animation time t runs from 0 (start) to 1 (wake-up time).

  wzBodenstrips        on at t = 0     1 -> 254 over t = 0 ... 1
  wzSchreibtischlampe  on at t = 1/3   1 -> 254 over t = 1/3 ... 1
  wzDeckenlampe        on at t = 2/3   1 -> 254 over t = 2/3 ... 1

"0 %" is Z2M brightness 1 (on, lowest), never off. Brightness follows a
perceptual curve: equal steps in *perceived* lightness (CIE L*), so the dark
start does not look like a jump. Colour: one shared ramp 370 -> 153 mired.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.clock import LOCAL_TZ
from app.domain.phases import local_dt

LAMPS = (
    ("wzBodenstrips", 0.0),
    ("wzSchreibtischlampe", 1.0 / 3.0),
    ("wzDeckenlampe", 2.0 / 3.0),
)
LAMP_NAMES = tuple(name for name, _ in LAMPS)
ROOM = "Wohnzimmer"  # shown as Schlafzimmer
CEILING = "wzDeckenlampe"
MIRED_START = 370
MIRED_END = 153


@dataclass(frozen=True)
class SunriseSettings:
    enabled: bool = False  # toggle "Sonnenaufgang": runs every day while on
    wake: time = time(7, 0)  # the time the animation ENDS
    duration_min: int = 30

    def validate(self) -> list[str]:
        if not 5 <= self.duration_min <= 120:
            return ["Die Dauer muss zwischen 5 und 120 Minuten liegen."]
        return []

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "wake": self.wake.strftime("%H:%M"), "duration_min": self.duration_min}

    @classmethod
    def from_dict(cls, data: dict | None) -> "SunriseSettings":
        data = data or {}
        base = cls()
        wake = data.get("wake")
        if isinstance(wake, str):
            hours, minutes = wake.split(":")[:2]
            wake = time(int(hours), int(minutes))
        return cls(
            enabled=bool(data.get("enabled", base.enabled)),
            wake=wake or base.wake,
            duration_min=int(data.get("duration_min", base.duration_min)),
        )


def perceptual(p: float) -> float:
    """Relative luminance (0..1) for perceived lightness p (0..1), CIE 1976 L*."""
    p = max(0.0, min(1.0, p))
    lightness = 100.0 * p
    if lightness > 8.0:
        return ((lightness + 16.0) / 116.0) ** 3
    return lightness / 903.3


def lamp_brightness(t: float, start: float) -> int | None:
    """Z2M brightness of a lamp that comes on at `start`, or None while still off."""
    if t < start:
        return None
    progress = 1.0 if start >= 1.0 else (t - start) / (1.0 - start)
    return max(1, min(254, round(1 + 253 * perceptual(progress))))


def mired_at(t: float) -> int:
    t = max(0.0, min(1.0, t))
    return round(MIRED_START + (MIRED_END - MIRED_START) * t)


def frame(t: float) -> dict[str, int | None]:
    """{lamp: brightness or None (not yet on)} at animation time t."""
    return {name: lamp_brightness(t, start) for name, start in LAMPS}


@dataclass(frozen=True)
class Occurrence:
    day: date  # the date of the wake-up time
    start: datetime
    wake: datetime

    def t(self, now: datetime) -> float:
        total = self.wake.timestamp() - self.start.timestamp()
        return max(0.0, min(1.0, (now.timestamp() - self.start.timestamp()) / total))

    def key(self) -> str:
        return self.day.isoformat()


def occurrence_on(settings: SunriseSettings, day: date) -> Occurrence:
    wake = local_dt(day, settings.wake, LOCAL_TZ)
    start = (wake.astimezone(timezone.utc) - timedelta(minutes=settings.duration_min)).astimezone(LOCAL_TZ)
    return Occurrence(day, start, wake)


def relevant_occurrence(settings: SunriseSettings, now: datetime) -> Occurrence:
    """Today's run if it is not over yet (incl. the finishing window), else tomorrow's.

    "Over" = later than wake + duration: up to then a restart still applies
    the final state (requirements §7a Reliability).
    """
    today = now.astimezone(LOCAL_TZ).date()
    for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
        occ = occurrence_on(settings, day)
        window_end = occ.wake.astimezone(timezone.utc) + timedelta(minutes=settings.duration_min)
        if now.astimezone(timezone.utc) < window_end:
            return occ
    return occurrence_on(settings, today + timedelta(days=2))
