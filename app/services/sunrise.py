"""Sunrise wake-up runner (requirements §7a). Runs entirely server-side.

Lifecycle of one day's run, persisted per wake-up date in `sunrise_runs`:

    (none) --start--> running   (animation)  --wake--> final alle_kalt --> done
           --start--> fallback  (Schlafzimmer was already lit) --wake--> ceiling 100 % + alle_kalt --> done
    running/fallback --cancel--> cancelled (no final; lamps stay as they are)

The record is what makes restarts safe:
* restart during the animation -> record says "running": resume at the right
  t (without the "already lit?" check, which would see our own lamps);
* restart after the wake-up time but within `duration` -> apply the final state;
* no record but already past the start -> treat like a normal start (join in).

Problems (Z2M offline, MQTT down, clock not synchronised, a lamp that did not
follow even after a resend) are collected per run. A successful run shows
nothing; a run with problems leaves a warning until it is dismissed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from app.clock import LOCAL_TZ, Clock
from app.config import AppConfig
from app.domain.scenes import scene_targets
from app.domain.sunrise import (
    CEILING,
    LAMP_NAMES,
    MIRED_END,
    ROOM,
    Occurrence,
    SunriseSettings,
    frame,
    mired_at,
    occurrence_on,
    relevant_occurrence,
)
from app.mqtt.publisher import Publisher
from app.services.lights import LightService
from app.state import StateStore
from app.store import Store

log = logging.getLogger(__name__)

STEP_S = 5.0  # animation update interval (with an equal Zigbee transition)
TEST_STEP_S = 2.0
TEST_DURATION_S = 50.0  # test plays the animation in ~50 s, then the final state
T_FINAL = 3.0  # "fading in over a few seconds"
CONNECT_WAIT_S = 300.0  # at the wake-up time, wait this long for MQTT to come back
KEEP_RUNS_DAYS = 14


class SunriseService:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        lights: LightService,
        publisher: Publisher,
        store: Store,
        clock: Clock,
        *,
        connected: Callable[[], bool] = lambda: True,
        clock_synced: Callable[[], bool | None] = lambda: True,
    ) -> None:
        self.config = config
        self.state = state
        self.lights = lights
        self.publisher = publisher
        self.store = store
        self.clock = clock
        self._connected = connected
        self._clock_synced = clock_synced
        self.changed = asyncio.Event()
        self._current: asyncio.Task | None = None
        self._current_key: str | None = None
        self.on_schedule_change: Callable[[], None] = lambda: None  # wakes the phase scheduler
        # Called right after the final alle_kalt: marks the current phase as applied,
        # so the phase scheduler does not re-apply kalt a moment later (that would
        # pull the fallback ceiling lamp below 100 %).
        self.on_final_applied: Callable[[], None] = lambda: None

    # ================================================================== settings / records

    @property
    def settings(self) -> SunriseSettings:
        return SunriseSettings.from_dict(self.store.get("sunrise_settings"))

    def update_settings(self, data: dict) -> list[str]:
        new = SunriseSettings.from_dict({**self.settings.to_dict(), **data})
        errors = new.validate()
        if errors:
            return errors
        old = self.settings
        self.store.set("sunrise_settings", new.to_dict())
        if old.enabled and not new.enabled and self._current_key and self._current_key != "test":
            log.info("Sunrise toggle switched off during a run -> cancelling today's run")
            self._cancel_current()
        self.changed.set()
        self.on_schedule_change()  # the early day start may have changed
        return []

    def _runs(self) -> dict[str, dict]:
        return self.store.get("sunrise_runs", {})

    def _record(self, key: str) -> dict | None:
        return self._runs().get(key)

    def _save_record(self, key: str, record: dict) -> None:
        runs = self._runs()
        runs[key] = record
        cutoff = (self.clock.now().date() - timedelta(days=KEEP_RUNS_DAYS)).isoformat()
        runs = {k: v for k, v in runs.items() if k == "test" or k >= cutoff}
        self.store.set("sunrise_runs", runs)

    def warning(self) -> dict | None:
        return self.store.get("sunrise_warning")

    def dismiss_warning(self) -> None:
        self.store.delete("sunrise_warning")

    # ================================================================== queries for others

    def early_starts(self) -> dict[date, datetime]:
        """Wake-up times per day for the phase schedule (early day start).

        Past/current days: from the run records (a cancelled run does not start
        the day early - decision 2026-11-12). Future days: from the settings.
        """
        settings = self.settings
        today = self.clock.now().astimezone(LOCAL_TZ).date()
        runs = self._runs()
        wakes: dict[date, datetime] = {}
        for offset in range(-2, 3):
            day = today + timedelta(days=offset)
            record = runs.get(day.isoformat())
            if record is not None:
                if record["status"] != "cancelled":
                    wakes[day] = datetime.fromisoformat(record["wake"])
            elif settings.enabled and day >= today:
                wakes[day] = occurrence_on(settings, day).wake
        return wakes

    def room_blocked(self, room: str) -> bool:
        """True while the animation runs in `room` (its wall switch is ignored)."""
        return room == ROOM and bool(self.lights.locked & set(LAMP_NAMES))

    def status(self) -> dict:
        now = self.clock.now()
        settings = self.settings
        occ = relevant_occurrence(settings, now)
        record = self._record(occ.key())
        running = self._current_key is not None
        return {
            "settings": settings.to_dict(),
            "running": running,
            "test": self._current_key == "test",
            "mode": self._record(self._current_key).get("mode") if running and self._record(self._current_key) else None,
            "next": {
                "start": occ.start.isoformat(timespec="seconds"),
                "wake": occ.wake.isoformat(timespec="seconds"),
            } if settings.enabled and not (record and record["status"] in ("done", "cancelled")) else None,
            "today": record,
            "warning": self.warning(),
        }

    # ================================================================== actions

    def cancel(self) -> bool:
        """Stop today's run: no further animation, no final alle_kalt."""
        if self._current_key is None:
            return False
        self._cancel_current()
        return True

    def _cancel_current(self) -> None:
        key = self._current_key
        record = self._record(key) if key else None
        if record is not None:
            record["status"] = "cancelled"
            record["finished_at"] = self.clock.now().isoformat(timespec="seconds")
            self._save_record(key, record)
        log.info("Sunrise %s cancelled", key)
        if self._current:
            self._current.cancel()
        self._release_locks()
        self.on_schedule_change()

    def start_test(self) -> bool:
        if self._current_key is not None:
            return False
        now = self.clock.now()
        occ = Occurrence(now.date(), now, now + timedelta(seconds=TEST_DURATION_S))
        self._launch("test", occ, kind="test")
        return True

    def _launch(self, key: str, occ: Occurrence, *, kind: str) -> asyncio.Task:
        self._current_key = key
        self._current = asyncio.create_task(self._execute(key, occ, kind), name=f"sunrise-{key}")
        return self._current

    def _release_locks(self) -> None:
        self.lights.locked -= set(LAMP_NAMES)

    # ================================================================== main loop

    async def run(self) -> None:
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - this loop must survive anything
                log.exception("Sunrise loop error")
                await self.clock.sleep(10)

    async def _step(self) -> None:
        if self._current is not None:
            # A run (real or test) is in progress: wait for it or a change.
            await self._wait_for(self._current)
            return
        now = self.clock.now()
        settings = self.settings

        # An unfinished run from before a restart has priority (it keeps its own times).
        resumable = self._resumable(now)
        if resumable is not None:
            key, occ = resumable
            log.info("Resuming sunrise %s", key)
            await self._wait_for(self._launch(key, occ, kind="normal"))
            return

        if not settings.enabled:
            await self._wait_until(None)
            return
        occ = relevant_occurrence(settings, now)
        record = self._record(occ.key())
        if record and record["status"] in ("done", "cancelled"):
            window_end = occ.wake.astimezone(timezone.utc) + timedelta(minutes=settings.duration_min)
            await self._wait_until(window_end)
            return
        if now < occ.start:
            await self._wait_until(occ.start)
            return
        await self._wait_for(self._launch(occ.key(), occ, kind="normal"))

    def _resumable(self, now: datetime) -> tuple[str, Occurrence] | None:
        for key, record in self._runs().items():
            if key == "test" or record["status"] not in ("running", "fallback"):
                continue
            start, wake = datetime.fromisoformat(record["start"]), datetime.fromisoformat(record["wake"])
            duration = wake.timestamp() - start.timestamp()
            if now.timestamp() < wake.timestamp() + duration:
                return key, Occurrence(date.fromisoformat(key), start, wake)
            # Too late to finish: give up on it, but say so.
            record["status"] = "missed"
            record.setdefault("problems", []).append("App war zum Weckzeitpunkt nicht aktiv")
            self._save_record(key, record)
            self._warn(key, record["problems"])
        return None

    async def _wait_until(self, when: datetime | None) -> None:
        """Sleep until `when` (or forever) unless settings change first."""
        waiter = asyncio.ensure_future(self.changed.wait())
        tasks = {waiter}
        if when is not None:
            tasks.add(asyncio.ensure_future(self.clock.sleep_until(when)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
        self.changed.clear()

    async def _wait_for(self, task: asyncio.Task) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise  # we ourselves are being cancelled (shutdown)
        except Exception:  # noqa: BLE001
            log.exception("Sunrise run failed")

    # ================================================================== one run

    async def _execute(self, key: str, occ: Occurrence, kind: str) -> None:
        test = kind == "test"
        record = self._record(key) if not test else None
        try:
            if record is None or record["status"] not in ("running", "fallback"):
                record = self._new_record(occ, kind)
                if test:
                    record["mode"] = "animation"
                elif self.clock.now() >= occ.wake:
                    record["mode"] = "animation"  # app was down during the animation: just finish
                elif any(self.lights.is_on(d.friendly_name) for d in self.config.lamps(ROOM)):
                    record["mode"] = "fallback"
                    record["status"] = "fallback"
                    log.info("Sunrise %s: Schlafzimmer already lit -> fallback at %s", key, occ.wake.strftime("%H:%M"))
                self._save_record(key, record)
                self.on_schedule_change()

            if record["mode"] == "animation":
                await self._animate(key, occ, record, step=TEST_STEP_S if test else STEP_S)
            else:
                await self.clock.sleep_until(occ.wake)

            await self._final(key, record)
        finally:
            if self._current_key == key:
                self._current_key = None
                self._current = None
            self._release_locks()

    def _new_record(self, occ: Occurrence, kind: str) -> dict:
        problems = []
        if self.state.bridge_online is not True:
            problems.append("Zigbee2MQTT war beim Start offline")
        if not self._connected():
            problems.append("Keine MQTT-Verbindung beim Start")
        if self._clock_synced() is False:
            problems.append("Systemuhr nicht synchronisiert")
        return {
            "kind": kind,
            "start": occ.start.isoformat(timespec="seconds"),
            "wake": occ.wake.isoformat(timespec="seconds"),
            "status": "running",
            "mode": "animation",
            "problems": problems,
            "began_at": self.clock.now().isoformat(timespec="seconds"),
        }

    async def _animate(self, key: str, occ: Occurrence, record: dict, *, step: float) -> None:
        self.lights.locked |= set(LAMP_NAMES)
        started: set[str] = set()
        last_sent: dict[str, tuple[int, int]] = {}
        verify_task: asyncio.Task | None = None
        log.info("Sunrise %s: animation %s -> %s", key, occ.start.strftime("%H:%M:%S"), occ.wake.strftime("%H:%M:%S"))
        while True:
            now = self.clock.now()
            t = occ.t(now)
            if t >= 1.0:
                break
            mired = mired_at(t)
            for name, brightness in frame(t).items():
                if brightness is None:
                    continue
                if name not in started:
                    # First command: switch on at its current level with the right colour, no fade.
                    ok = await self.publisher.send(
                        name, {"state": "ON", "brightness": brightness, "color_temp": mired, "transition": 0}
                    )
                    started.add(name)
                    last_sent[name] = (brightness, mired)
                    if not ok:
                        record["problems"].append(f"{name}: Startbefehl nicht gesendet")
                    if name == "wzBodenstrips" and verify_task is None:
                        verify_task = asyncio.create_task(self.lights.verify_critical({name: True}))
                    continue
                if last_sent.get(name) == (brightness, mired):
                    continue
                await self.publisher.send(name, {"brightness": brightness, "color_temp": mired, "transition": step})
                last_sent[name] = (brightness, mired)
            remaining = occ.wake.timestamp() - now.timestamp()
            await self.clock.sleep(min(step, max(remaining, 0.0)))
        if verify_task is not None:
            failed = await verify_task
            record["problems"].extend(f"{n}: reagierte nicht auf den Start" for n in failed)
        self._save_record(key, record)

    async def _final(self, key: str, record: dict) -> None:
        # MQTT down right now? Wait a while for it instead of silently doing nothing.
        if not self._connected():
            record["problems"].append("Keine MQTT-Verbindung zum Weckzeitpunkt")
            waited = 0.0
            while not self._connected() and waited < CONNECT_WAIT_S:
                await self.clock.sleep(2)
                waited += 2
        self._release_locks()
        expected: dict[str, bool] = {}
        if record["mode"] == "fallback":
            # Ceiling lamp first, always sent, 100 % and coldest; it keeps that through alle_kalt.
            await self.publisher.send(
                CEILING, {"state": "ON", "brightness": 254, "color_temp": MIRED_END, "transition": T_FINAL}
            )
            self.lights.locked.add(CEILING)
            expected[CEILING] = True
        # A real run ends in the morning where the master colour is cold anyway.
        # The test may run in the evening: show the morning look (coldest); the
        # normal colour sync glides back to the current colour afterwards.
        final_mired = MIRED_END if record.get("kind") == "test" else None
        try:
            for room in self.config.rooms:
                await self.lights.apply_scene(room, "kalt", transition=T_FINAL, explicit=True, mired=final_mired)
                for name, target in scene_targets(self.config, room, "kalt").items():
                    if name not in self.lights.locked:
                        expected[name] = target.on
            self.on_final_applied()
        finally:
            self.lights.locked.discard(CEILING)
        log.info("Sunrise %s: final alle_kalt applied (%s)", key, record["mode"])

        failed = await self.lights.verify_critical(expected)
        record["problems"].extend(f"{n}: folgte nicht (alle_kalt)" for n in failed)
        record["status"] = "done"
        record["finished_at"] = self.clock.now().isoformat(timespec="seconds")
        self._save_record(key, record)
        if record["problems"]:
            self._warn(key, record["problems"])
        else:
            log.info("Sunrise %s: success", key)

    def _warn(self, key: str, problems: list[str]) -> None:
        log.warning("Sunrise %s had problems: %s", key, "; ".join(problems))
        self.store.set(
            "sunrise_warning",
            {"run": key, "at": self.clock.now().isoformat(timespec="seconds"), "problems": list(problems)},
        )
