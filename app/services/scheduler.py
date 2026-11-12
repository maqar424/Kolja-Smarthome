"""The automation loop: phase changes and colour follow.

Every tick:
1. Compute the phase now. If it differs from the phase the lamps were last
   switched to (persisted), lit rooms fade to the new scene. This also catches
   a boundary that passed while the app was down.
2. Bring the colour of all lit lamps to the current master colour.

Ticks happen every 30 s, exactly at the next phase boundary, and immediately
after a change (manual advance, slider, settings).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.clock import Clock
from app.services.lights import T_COLOUR, LightService
from app.services.schedule import Schedule
from app.store import Store

log = logging.getLogger(__name__)

TICK_S = 30.0
T_JUMP = 5.0  # colour after a manual advance / slider: "within a few seconds"


class Scheduler:
    def __init__(self, schedule: Schedule, lights: LightService, store: Store, clock: Clock) -> None:
        self.schedule = schedule
        self.lights = lights
        self.store = store
        self.clock = clock
        self.heartbeat: datetime | None = None

    async def run(self) -> None:
        prompt = False
        while True:
            try:
                await self.tick(prompt=prompt)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("Scheduler tick failed")
            prompt = await self._wait()

    async def tick(self, *, prompt: bool = False) -> None:
        now = self.clock.now()
        timeline = self.schedule.timeline(now)
        phase = timeline.phase
        applied = self.store.applied_phase()
        last = timeline.last_boundary(now)
        boundary_at = last.at if last else now

        if applied is None:
            # First start ever: remember the phase, don't touch any lamp.
            log.info("First start: current phase %s", phase)
            self.store.set_applied_phase(phase, boundary_at)
        elif applied["phase"] != phase:
            log.info("Phase change %s -> %s (%s)", applied["phase"], phase, last.source if last else "?")
            await self.lights.on_phase_change(phase)
            self.store.set_applied_phase(phase, boundary_at)

        # Right after a change (button, slider, settings) the colour should
        # follow within a few seconds; otherwise glide smoothly between ticks.
        await self.lights.sync_colour(T_JUMP if prompt else T_COLOUR)
        self.heartbeat = now

    async def _wait(self) -> bool:
        """Sleep until the next tick. True if woken by a change (respond quickly)."""
        now = self.clock.now()
        nxt = self.schedule.timeline(now).next_boundary(now)
        wake = now.astimezone(timezone.utc) + timedelta(seconds=TICK_S)
        if nxt is not None and nxt.at.astimezone(timezone.utc) < wake:
            wake = nxt.at.astimezone(timezone.utc)
        changed = self.schedule.changed
        sleeper = asyncio.ensure_future(self.clock.sleep_until(wake))
        waiter = asyncio.ensure_future(changed.wait())
        try:
            done, _ = await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waiter.cancel()
        if changed.is_set():
            changed.clear()
            return True
        return False
