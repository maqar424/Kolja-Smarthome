"""Injectable clock.

All time-based logic asks a Clock for "now" and sleeps through it. Production
uses RealClock; tests use FakeClock and jump through hours or days instantly
(colour ramps, phase changes, sunrise timeline, restart/resume).

DST pitfall: for aware datetimes sharing a ZoneInfo, Python's `+ timedelta`
and `a - b` work on *wall-clock* time and ignore the UTC offset change. Do
duration arithmetic in UTC (`dt.astimezone(timezone.utc)`), use local time
only to build "today at 07:00"-style moments.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Berlin")


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware (Europe/Berlin)."""

    async def sleep(self, seconds: float) -> None: ...

    async def sleep_until(self, when: datetime) -> None: ...


class RealClock:
    def __init__(self, tz: ZoneInfo = LOCAL_TZ) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, when: datetime) -> None:
        # Sleep in chunks of at most 30 s: if the system clock jumps (NTP sync
        # after boot), we notice within half a minute instead of oversleeping.
        target = when.astimezone(timezone.utc)  # UTC, see module docstring
        while True:
            remaining = (target - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 30.0))


class FakeClock:
    """Manually advanced clock for tests.

    Tasks sleeping on the clock wake up in time order when `advance()` moves
    time past their deadline, and get a chance to run before `advance()` returns.
    """

    def __init__(self, start: datetime, tz: ZoneInfo = LOCAL_TZ) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        self.tz = tz
        self._now = start.astimezone(timezone.utc)
        self._sleepers: list[tuple[datetime, int, asyncio.Future]] = []
        self._counter = itertools.count()

    def now(self) -> datetime:
        return self._now.astimezone(self.tz)

    async def sleep(self, seconds: float) -> None:
        # Arithmetic in UTC: adding a timedelta to an aware *local* datetime is
        # wall-clock arithmetic in Python and goes wrong across DST switches.
        await self.sleep_until(self._now + timedelta(seconds=max(0.0, seconds)))

    async def sleep_until(self, when: datetime) -> None:
        if when <= self._now:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (when.astimezone(timezone.utc), next(self._counter), future))
        await future

    def set(self, when: datetime) -> None:
        """Jump without waking anybody (use before tasks are started)."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.tz)
        self._now = when.astimezone(timezone.utc)

    async def advance(self, seconds: float = 0.0, *, to: datetime | None = None) -> None:
        target = to if to is not None else self._now + timedelta(seconds=seconds)  # UTC, see sleep()
        if target.tzinfo is None:
            target = target.replace(tzinfo=self.tz)
        target = target.astimezone(timezone.utc)
        await _settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            when, _, future = heapq.heappop(self._sleepers)
            self._now = max(self._now, when)
            if not future.done():
                future.set_result(None)
            await _settle()
        self._now = max(self._now, target)
        await _settle()


async def _settle(rounds: int = 20) -> None:
    """Let ready tasks run until they block again."""
    for _ in range(rounds):
        await asyncio.sleep(0)
