"""Global colour: master_ct in [0, 1] (0 = coldest, 1 = warmest) and its schedule.

The current value is computed, not stored: it is the result of folding all
colour events up to `now` in time order. After a restart the app therefore
lands on exactly the right value.

Events
  ramp      starts at a phase boundary: from the *current* value to the target
            over a duration (warm: -> 1 over 60 min, kalt: -> 0 over 30 min)
  jump      immediate change (manual "next phase", early day start by sunrise)
  override  master slider; holds until the next ramp or jump starts
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.phases import Boundary
from app.domain.settings import ScheduleSettings

MIRED_COLD = 153
MIRED_WARM = 370

# Colour of each phase when jumping to it (manual advance).
PHASE_COLOUR = {"kalt": 0.0, "warm": 1.0, "twilight": 1.0, "night": 1.0}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def to_mired(master_ct: float) -> int:
    """Linear in mired across the common range 153-370."""
    return round(MIRED_COLD + clamp01(master_ct) * (MIRED_WARM - MIRED_COLD))


def from_mired(mired: float) -> float:
    return clamp01((mired - MIRED_COLD) / (MIRED_WARM - MIRED_COLD))


@dataclass(frozen=True)
class ColourEvent:
    at: datetime
    kind: str  # "ramp" | "jump" | "override"
    target: float
    duration_s: float = 0.0


def events_from_boundaries(boundaries: list[Boundary], settings: ScheduleSettings) -> list[ColourEvent]:
    events = []
    for b in boundaries:
        if b.source == "manual":
            events.append(ColourEvent(b.at, "jump", PHASE_COLOUR[b.phase]))
        elif b.phase == "kalt" and b.source == "wake":
            events.append(ColourEvent(b.at, "jump", 0.0))
        elif b.phase == "kalt":
            events.append(ColourEvent(b.at, "ramp", 0.0, settings.kalt_ramp_min * 60.0))
        elif b.phase == "warm":
            events.append(ColourEvent(b.at, "ramp", 1.0, settings.warm_ramp_min * 60.0))
    return events


@dataclass(frozen=True)
class ColourState:
    master_ct: float
    ramping: bool  # a ramp is in progress at this moment
    override: bool  # the slider value is in effect
    since: datetime | None  # when the deciding event happened


def colour_at(events: list[ColourEvent], t: datetime, initial: float = 1.0) -> ColourState:
    """Fold events (any order) up to and including `t`."""
    value = clamp01(initial)
    ramp: tuple[float, ColourEvent] | None = None  # (start value, event)
    last: ColourEvent | None = None
    for event in sorted(events, key=lambda e: e.at):
        if event.at > t:
            break
        if ramp is not None:
            value = _ramp_value(ramp, event.at)
            ramp = None
        if event.kind == "ramp" and event.duration_s > 0:
            ramp = (value, event)
        else:  # jump, override, or a zero-length ramp
            value = clamp01(event.target)
        last = event
    ramping = False
    if ramp is not None:
        value = _ramp_value(ramp, t)
        ramping = _elapsed(ramp[1].at, t) < ramp[1].duration_s
    return ColourState(
        master_ct=value,
        ramping=ramping,
        override=last is not None and last.kind == "override",
        since=last.at if last else None,
    )


def _elapsed(start: datetime, t: datetime) -> float:
    return t.timestamp() - start.timestamp()  # timestamps: DST-safe


def _ramp_value(ramp: tuple[float, ColourEvent], t: datetime) -> float:
    start_value, event = ramp
    progress = min(1.0, max(0.0, _elapsed(event.at, t) / event.duration_s))
    return start_value + (event.target - start_value) * progress
