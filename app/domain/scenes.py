"""Scenes and dimming (requirements §5, §6).

Brightness unit here is the Z2M scale 0-254. Scene percentages from the sheet
are converted with pct / 100 * 254.

Each room has
  scene   the active scene key (kalt/warm/twilight/night)
  factor  the dim factor f (1 = scene brightness)
  bases   per dimmable lamp the brightness at f = 1 ("base"). Normally the
          scene value; a lamp set by hand from the app gets base = level / f,
          so dimming keeps ratios for it too.
Lamp brightness = base * f, rounded, never below 1 (dimming never turns off).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import AppConfig

Z2M_MAX = 254
DIM_UP = 1.15
DIM_DOWN = 0.85


@dataclass(frozen=True)
class LampTarget:
    on: bool
    brightness: int | None = None  # None for on/off-only lamps or when off


@dataclass
class RoomLight:
    scene: str | None = None
    factor: float = 1.0
    bases: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"scene": self.scene, "factor": self.factor, "bases": dict(self.bases)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "RoomLight":
        data = data or {}
        return cls(
            scene=data.get("scene"),
            factor=float(data.get("factor", 1.0)),
            bases={k: float(v) for k, v in (data.get("bases") or {}).items()},
        )


def pct_to_level(pct: float) -> float:
    return pct / 100.0 * Z2M_MAX


def level(base: float, factor: float) -> int:
    return max(1, min(Z2M_MAX, round(base * factor)))


def scene_bases(config: AppConfig, room: str, scene: str) -> dict[str, float]:
    """Base brightness (0-254, unrounded) of each dimmable lamp that is on in the scene."""
    grid = config.brightness.get(scene, {})
    bases = {}
    for lamp in config.lamps(room):
        value = grid.get(lamp.friendly_name)
        if lamp.dimmable and value is not None and value.on and value.pct is not None:
            bases[lamp.friendly_name] = pct_to_level(value.pct)
    return bases


def scene_targets(config: AppConfig, room: str, scene: str, factor: float = 1.0) -> dict[str, LampTarget]:
    """What every lamp of the room should do for `scene` (full apply, incl. off)."""
    grid = config.brightness.get(scene, {})
    targets = {}
    for lamp in config.lamps(room):
        value = grid.get(lamp.friendly_name)
        if value is None or not value.on:
            targets[lamp.friendly_name] = LampTarget(on=False)
        elif lamp.dimmable and value.pct is not None:
            targets[lamp.friendly_name] = LampTarget(on=True, brightness=level(pct_to_level(value.pct), factor))
        else:
            targets[lamp.friendly_name] = LampTarget(on=True)
    return targets


def next_factor(bases_on: list[float], factor: float, direction: str) -> float | None:
    """New dim factor after one press, or None if the press changes nothing.

    bases_on: base brightness of the dimmable lamps that are currently on.
    up:   x1.15, but stop exactly when the brightest lamp reaches 100 %.
    down: x0.85, unless that would round any lamp to 0 (then stay).
    """
    bases = [b for b in bases_on if b > 0]
    if not bases:
        return None
    if direction == "up":
        cap = Z2M_MAX / max(bases)
        if factor >= cap - 1e-9 or level(max(bases), factor) >= Z2M_MAX:
            return None
        return min(factor * DIM_UP, cap)
    if direction == "down":
        new = factor * DIM_DOWN
        if any(round(b * new) < 1 for b in bases):
            return None
        return new
    raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
