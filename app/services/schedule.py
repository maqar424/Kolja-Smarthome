"""Current phase and colour, computed from stored settings and events."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Callable

from app.clock import Clock
from app.domain.colour import ColourState, clamp01, colour_at, events_from_boundaries, to_mired
from app.domain.phases import ManualAdvance, PhaseTimeline
from app.domain.settings import ScheduleSettings
from app.store import Store

log = logging.getLogger(__name__)


class Schedule:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        wakes: Callable[[], dict[date, datetime]] = dict,
    ) -> None:
        self._store = store
        self._clock = clock
        self._wakes = wakes  # provided by the sunrise service (early day start)
        self.changed = asyncio.Event()  # set on every change; the scheduler loop waits on it

    # ------------------------------------------------------------------ queries

    @property
    def settings(self) -> ScheduleSettings:
        return self._store.schedule()

    def timeline(self, now: datetime | None = None) -> PhaseTimeline:
        now = now or self._clock.now()
        return PhaseTimeline(self.settings, now, self._store.manual_advances(), self._wakes())

    def colour(self, now: datetime | None = None, timeline: PhaseTimeline | None = None) -> ColourState:
        now = now or self._clock.now()
        timeline = timeline or self.timeline(now)
        events = events_from_boundaries(timeline.boundaries, timeline.settings)
        override = self._store.colour_override()
        if override is not None:
            events.append(override)
        return colour_at(events, now)

    def phase(self) -> str:
        return self.timeline().phase

    def mired(self) -> int:
        return to_mired(self.colour().master_ct)

    # ------------------------------------------------------------------ actions

    def advance(self) -> str | None:
        """Manual "next phase". Returns the new phase, or None in night."""
        now = self._clock.now()
        timeline = self.timeline(now)
        target = timeline.next_phase()
        if target is None:
            return None
        self._store.add_manual_advance(ManualAdvance(now, target))
        self._store.clear_colour_override()  # the jump supersedes it anyway; keep the UI clean
        log.info("Manual advance %s -> %s", timeline.phase, target)
        self.changed.set()
        return target

    def back(self) -> str | None:
        """Manual "previous phase". Returns the new phase, or None in kalt."""
        now = self._clock.now()
        timeline = self.timeline(now)
        target = timeline.previous_phase()
        if target is None:
            return None
        self._store.add_manual_advance(ManualAdvance(now, target))
        self._store.clear_colour_override()
        log.info("Manual step back %s -> %s", timeline.phase, target)
        self.changed.set()
        return target

    def resume_schedule(self) -> None:
        """Forget manual phase changes and the colour override: back to the plan."""
        self._store.clear_manual_advances()
        self._store.clear_colour_override()
        log.info("Back to schedule")
        self.changed.set()

    def set_override(self, master_ct: float) -> None:
        self._store.set_colour_override(self._clock.now(), clamp01(master_ct))
        self.changed.set()

    def clear_override(self) -> None:
        self._store.clear_colour_override()
        self.changed.set()

    def update_settings(self, data: dict) -> list[str]:
        new = ScheduleSettings.from_dict({**self.settings.to_dict(), **data})
        errors = new.validate()
        if not errors:
            self._store.save_schedule(new)
            self.changed.set()
        return errors
