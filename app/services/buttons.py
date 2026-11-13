"""Wall switches (Hue dimmer switch gen 2) and the kettle button (requirements §7).

Everything here is behind BUTTONS_ENABLED (default off), so the old
smarthome_hub and this app never both react to a press.

Wall switch, one per room:
  on_press    room off -> current phase scene; room on -> re-apply it (f = 1)
  up_press    dim up        down_press  dim down
  off_press   room off (lamps with in_room_off)
  Holds, releases and recall_* are ignored, except for the Flur rule below.

Flur hold: the press acts immediately. 1.5 s after the press, "all on" (top)
or "all off" (bottom) fires if a *_hold for that button arrived after the
press and no *_release since. If neither has arrived yet, we wait up to 0.5 s
longer for a late hold message (Zigbee latency). A missing release alone is
never taken as "still held".

Kettle: kuecheButton press -> kuecheWasserkocher on, off after 180 s; a press
while it runs switches it off at once (toggle). One physical press sends
press and on/off at the same instant (release follows, ignored), so messages
within 0.8 s count as one press. Quick double presses still work.
The off time is persisted. Once started, the timer always completes (safety),
even if buttons are disabled in between.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.clock import Clock
from app.config import AppConfig
from app.mqtt.publisher import Publisher
from app.services.lights import LightService
from app.state import ActionEvent, StateStore
from app.store import Store

log = logging.getLogger(__name__)

DIMMER_MODEL = "929002398602"  # Hue dimmer switch gen 2
FLUR_SWITCH = "flurLichtschalter"
HOLD_AFTER_S = 1.5
HOLD_GRACE_S = 0.5

KETTLE_BUTTON = "kuecheButton"
KETTLE_PLUG = "kuecheWasserkocher"
KETTLE_ON_S = 180.0
KETTLE_DEBOUNCE_S = 0.8  # press + on/off arrive together; 3 s felt like a cooldown
KETTLE_ACTIONS = {"press", "on", "off"}


@dataclass
class _PendingHold:
    button: str  # "on" | "off"
    pressed_at: float
    hold_seen: bool = False
    released: bool = False
    task: asyncio.Task | None = None


class ButtonService:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        lights: LightService,
        publisher: Publisher,
        store: Store,
        clock: Clock,
        *,
        enabled: bool,
        room_blocked: Callable[[str], bool] = lambda room: False,
    ) -> None:
        self.config = config
        self.lights = lights
        self.publisher = publisher
        self.store = store
        self.clock = clock
        self.enabled = enabled
        self._room_blocked = room_blocked  # sunrise running in this room (M4)
        self.switch_room = {
            d.friendly_name: d.room
            for d in config.devices.values()
            if d.category == "remote" and d.model == DIMMER_MODEL and d.room
        }
        self._hold: _PendingHold | None = None
        self._kettle_task: asyncio.Task | None = None
        self._kettle_last_press = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()  # handle presses one after another, in arrival order
        state.subscribe(self._on_event)

    # ------------------------------------------------------------------ plumbing

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _on_event(self, event) -> None:
        if not isinstance(event, ActionEvent):
            return
        if event.name not in self.switch_room and event.name != KETTLE_BUTTON:
            return
        if not self.enabled:
            log.info("Button %s %s ignored (BUTTONS_ENABLED=false)", event.name, event.action)
            return
        self._spawn(self.handle(event))

    # ------------------------------------------------------------------ dispatch

    async def handle(self, event: ActionEvent) -> None:
        async with self._lock:
            try:
                if event.name == KETTLE_BUTTON:
                    await self._kettle_press(event)
                else:
                    await self._switch(event)
            except Exception:  # noqa: BLE001 - one bad press must not stop the next
                log.exception("Handling %s %s failed", event.name, event.action)

    async def _switch(self, event: ActionEvent) -> None:
        room = self.switch_room[event.name]
        action = event.action
        if event.name == FLUR_SWITCH:
            self._track_hold(event)
        if self._room_blocked(room):
            log.info("%s %s ignored: sunrise running in %s", event.name, action, room)
            return
        if action == "on_press":
            log.info("%s: top -> room on (%s)", event.name, room)
            await self.lights.room_on(room)
        elif action == "off_press":
            log.info("%s: bottom -> room off (%s)", event.name, room)
            await self.lights.room_off(room)
        elif action == "up_press":
            await self.lights.dim(room, "up")
        elif action == "down_press":
            await self.lights.dim(room, "down")
        # everything else (holds, releases, recall_*) is ignored on purpose

    # ------------------------------------------------------------------ Flur hold

    def _track_hold(self, event: ActionEvent) -> None:
        button, _, kind = event.action.partition("_")  # "on_press" -> ("on", "press")
        if button not in ("on", "off"):
            return
        pending = self._hold
        if kind == "press":
            if pending and pending.task:
                pending.task.cancel()
            pending = _PendingHold(button, event.at)
            self._hold = pending
            pending.task = self._spawn(self._hold_timer(pending))
        elif pending and pending.button == button and event.at >= pending.pressed_at:
            if kind == "hold":
                pending.hold_seen = True
            elif kind in ("press_release", "hold_release"):
                pending.released = True

    async def _hold_timer(self, pending: _PendingHold) -> None:
        start = datetime.fromtimestamp(pending.pressed_at, timezone.utc)
        await self.clock.sleep_until(start + timedelta(seconds=HOLD_AFTER_S))
        if not pending.hold_seen and not pending.released:
            # No news yet: give a late hold message a short chance.
            deadline = start + timedelta(seconds=HOLD_AFTER_S + HOLD_GRACE_S)
            while not pending.hold_seen and not pending.released:
                if self.clock.now() >= deadline:
                    break
                await self.clock.sleep(0.05)
        if self._hold is not pending or pending.released or not pending.hold_seen:
            return
        self._hold = None
        async with self._lock:
            if pending.button == "on":
                log.info("Flur: top held -> all on")
                await self.lights.all_on()
            else:
                log.info("Flur: bottom held -> all off")
                await self.lights.all_off()

    # ------------------------------------------------------------------ kettle

    async def _kettle_press(self, event: ActionEvent) -> None:
        if event.action not in KETTLE_ACTIONS:
            return
        if event.at - self._kettle_last_press < KETTLE_DEBOUNCE_S:
            return  # one physical press sends several messages
        self._kettle_last_press = event.at
        if self.store.get("kettle_off_at"):
            # Second press while running: stop early (decision 2026-11-12).
            log.info("Kettle button: kettle running -> off now")
            if self._kettle_task:
                self._kettle_task.cancel()
                self._kettle_task = None
            await self._kettle_off()
            return
        off_at = self.clock.now() + timedelta(seconds=KETTLE_ON_S)
        log.info("Kettle button: kettle on until %s", off_at.strftime("%H:%M:%S"))
        self.store.set("kettle_off_at", off_at.astimezone(timezone.utc).isoformat())
        await self.publisher.send(KETTLE_PLUG, {"state": "ON"})
        self._start_kettle_timer(off_at)

    def _start_kettle_timer(self, off_at: datetime) -> None:
        if self._kettle_task:
            self._kettle_task.cancel()
        self._kettle_task = self._spawn(self._kettle_timer(off_at))

    async def _kettle_timer(self, off_at: datetime) -> None:
        await self.clock.sleep_until(off_at)
        log.info("Kettle timer done: kettle off")
        await self._kettle_off()

    async def _kettle_off(self) -> None:
        # Retry until the command is out (e.g. MQTT still reconnecting after a restart).
        while not await self.publisher.send(KETTLE_PLUG, {"state": "OFF"}):
            log.warning("Kettle off not sent yet - retrying in 5 s")
            await self.clock.sleep(5)
        self.store.delete("kettle_off_at")

    def resume(self) -> None:
        """After a restart: finish a kettle timer that was running (always, for safety)."""
        stored = self.store.get("kettle_off_at")
        if stored:
            off_at = datetime.fromisoformat(stored)
            log.info("Resuming kettle timer (off at %s)", off_at.astimezone(self.clock.now().tzinfo).strftime("%H:%M:%S"))
            self._start_kettle_timer(off_at)
