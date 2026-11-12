"""Day phases: kalt -> warm -> twilight -> night, with sun mode, an early day
start from the sunrise wake-up, and the manual "next phase" button.

How "never steps backwards" works
---------------------------------
We walk through all boundaries (scheduled ones and manual advances) in time
order and keep track of the current phase:

* a manual advance always applies (it only ever moves one step forward);
* a scheduled boundary into `kalt` always applies: it starts a new day;
* any other scheduled boundary applies only if it moves the phase *forward*.

Example: advanced to night at 20:00 -> the scheduled twilight (21:00) and
night (22:30) do not move forward, so they are skipped; kalt at 07:00 the next
morning applies again.

The result is the list of boundaries that actually happened (or will happen,
if nobody presses the button). Phase bar, current phase, next boundary and the
colour schedule are all derived from this one list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

from app.clock import LOCAL_TZ
from app.domain.settings import PHASES, ScheduleSettings, SunRule

PHASE_INDEX = {p: i for i, p in enumerate(PHASES)}


@dataclass(frozen=True)
class Boundary:
    at: datetime
    phase: str
    source: str  # "schedule" | "wake" (early day start) | "manual"


@dataclass(frozen=True)
class ManualAdvance:
    at: datetime
    to: str


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    phase: str


# --------------------------------------------------------------------------- time helpers


def local_dt(day: date, t: time, tz: ZoneInfo = LOCAL_TZ) -> datetime:
    """`day` at wall-clock time `t`. Normalised through UTC so a time inside
    the DST gap (e.g. 02:30 on the spring-forward night) becomes a real instant."""
    return datetime.combine(day, t, tzinfo=tz).astimezone(timezone.utc).astimezone(tz)


def sunset(settings: ScheduleSettings, day: date, tz: ZoneInfo = LOCAL_TZ) -> datetime:
    observer = Observer(latitude=settings.latitude, longitude=settings.longitude)
    return sun(observer, date=day, tzinfo=tz)["sunset"]


def _sun_rule_time(rule: SunRule, settings: ScheduleSettings, day: date, tz: ZoneInfo) -> datetime:
    try:
        candidate = sunset(settings, day, tz) + timedelta(minutes=rule.offset_min)
    except ValueError:  # no sunset (polar day/night) - not an issue in Köln, but be safe
        candidate = local_dt(day, rule.earliest, tz)
    earliest, latest = local_dt(day, rule.earliest, tz), local_dt(day, rule.latest, tz)
    return min(max(candidate, earliest), latest)


# --------------------------------------------------------------------------- schedule


def scheduled_boundaries(
    settings: ScheduleSettings,
    day: date,
    wake: datetime | None = None,
    tz: ZoneInfo = LOCAL_TZ,
) -> list[Boundary]:
    """The four planned phase starts on `day`.

    `wake`: sunrise wake-up time on this day if it is earlier than kalt; the
    day then starts at the wake-up time (source "wake").
    """
    kalt = local_dt(day, settings.kalt, tz)
    kalt_source = "schedule"
    if wake is not None and wake < kalt:
        kalt, kalt_source = wake, "wake"

    if settings.sun_mode:
        warm = _sun_rule_time(settings.warm_sun, settings, day, tz)
        twilight = _sun_rule_time(settings.twilight_sun, settings, day, tz)
    else:
        warm = local_dt(day, settings.warm, tz)
        twilight = local_dt(day, settings.twilight, tz)
    night = local_dt(day, settings.night, tz)

    # Keep the order even if sun rules and fixed times collide.
    warm = max(warm, kalt + timedelta(minutes=1))
    twilight = max(twilight, warm + timedelta(minutes=1))
    night = max(night, twilight + timedelta(minutes=1))
    return [
        Boundary(kalt, "kalt", kalt_source),
        Boundary(warm, "warm", "schedule"),
        Boundary(twilight, "twilight", "schedule"),
        Boundary(night, "night", "schedule"),
    ]


def effective_boundaries(
    settings: ScheduleSettings,
    first_day: date,
    last_day: date,
    manual: list[ManualAdvance] = (),
    wakes: dict[date, datetime] | None = None,
    tz: ZoneInfo = LOCAL_TZ,
) -> tuple[str, list[Boundary]]:
    """Walk the schedule from `first_day` 00:00 to the end of `last_day`.

    Returns (phase at the start of first_day, boundaries that apply). The
    phase at the start is "night" (the previous day's evening).
    """
    wakes = wakes or {}
    planned: list[Boundary] = []
    day = first_day
    while day <= last_day:
        planned.extend(scheduled_boundaries(settings, day, wakes.get(day), tz))
        day += timedelta(days=1)
    window_start = local_dt(first_day, time(0, 0), tz)
    planned.extend(Boundary(m.at, m.to, "manual") for m in manual if m.at >= window_start)
    # Manual advances sort after a scheduled boundary at the very same instant.
    planned.sort(key=lambda b: (b.at, b.source == "manual"))

    current = "night"
    applied: list[Boundary] = []
    for boundary in planned:
        if boundary.source == "manual":
            applies = PHASE_INDEX[boundary.phase] > PHASE_INDEX[current]
        elif boundary.phase == "kalt":
            applies = current != "kalt"
        else:
            applies = PHASE_INDEX[boundary.phase] > PHASE_INDEX[current]
        if applies:
            applied.append(boundary)
            current = boundary.phase
    return "night", applied


class PhaseTimeline:
    """Convenience wrapper: the effective boundaries around a given moment."""

    def __init__(
        self,
        settings: ScheduleSettings,
        now: datetime,
        manual: list[ManualAdvance] = (),
        wakes: dict[date, datetime] | None = None,
        tz: ZoneInfo = LOCAL_TZ,
    ) -> None:
        self.settings = settings
        self.now = now
        self.tz = tz
        today = now.astimezone(tz).date()
        # Two days back is enough history (every day restarts with kalt),
        # two days ahead covers "next boundary" after a manual advance to night.
        self.first_day = today - timedelta(days=2)
        self.last_day = today + timedelta(days=2)
        self.initial, self.boundaries = effective_boundaries(
            settings, self.first_day, self.last_day, manual, wakes, tz
        )

    def phase_at(self, t: datetime) -> str:
        phase = self.initial
        for b in self.boundaries:
            if b.at <= t:
                phase = b.phase
            else:
                break
        return phase

    def last_boundary(self, t: datetime) -> Boundary | None:
        last = None
        for b in self.boundaries:
            if b.at <= t:
                last = b
            else:
                break
        return last

    def next_boundary(self, t: datetime) -> Boundary | None:
        return next((b for b in self.boundaries if b.at > t), None)

    @property
    def phase(self) -> str:
        return self.phase_at(self.now)

    def can_advance(self) -> bool:
        return self.phase != "night"

    def next_phase(self) -> str | None:
        index = PHASE_INDEX[self.phase]
        return PHASES[index + 1] if index + 1 < len(PHASES) else None

    def segments(self, day: date) -> list[Segment]:
        """Phase bar for `day`: 00:00 to 24:00 split into phase segments."""
        start = local_dt(day, time(0, 0), self.tz)
        end = local_dt(day + timedelta(days=1), time(0, 0), self.tz)
        segments: list[Segment] = []
        current_start, current_phase = start, self.phase_at(start)
        for b in self.boundaries:
            if start < b.at < end:
                if b.at > current_start:
                    segments.append(Segment(current_start, b.at, current_phase))
                current_start, current_phase = b.at, b.phase
        segments.append(Segment(current_start, end, current_phase))
        return segments
