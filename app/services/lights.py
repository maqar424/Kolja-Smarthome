"""Turns intentions ("room on", "dim up", "phase changed") into lamp commands.

Rules applied here (requirements §4-§7):
- A command that turns a lamp on carries brightness AND colour (no flash of an old colour).
- Colour goes per lamp, only to colour lamps that are on and not locked.
- Plugs get on/off only.
- Locked lamps (running sunrise) are never touched by anything here.
- Explicit user actions always send; automatic phase changes skip no-ops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from app.clock import Clock
from app.config import ALL_LAMPS_GROUP, AppConfig, Device
from app.domain.scenes import RoomLight, level, next_factor, scene_bases, scene_targets
from app.mqtt.publisher import Publisher, SentCommand
from app.state import StateChanged, StateStore
from app.store import Store

log = logging.getLogger(__name__)

# Transition times in seconds
T_BUTTON = 0.4  # wall switch / app on-off: should feel instant
T_DIM = 0.3
T_PHASE = 10.0  # fade to the new phase scene
T_COLOUR = 20.0  # colour follow during ramps (scheduler ticks every 30 s)
T_FIX = 0.0  # colour correction of a lamp turned on outside the app

EXTERNAL_ON_GRACE_S = 10.0  # an ON within this time after our command is ours
VERIFY_DELAY_S = 2.0
COLOUR_TOLERANCE = 1  # mired
COLOUR_RESEND_S = 120.0  # don't repeat the same colour to a lamp more often


class LightService:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        publisher: Publisher,
        store: Store,
        clock: Clock,
        *,
        phase: Callable[[], str],
        mired: Callable[[], int],
    ) -> None:
        self.config = config
        self.state = state
        self.publisher = publisher
        self.store = store
        self.clock = clock
        self._phase = phase
        self._mired = mired
        self.locked: set[str] = set()
        self._last_cmd_at: dict[str, float] = {}
        self._last_colour: dict[str, tuple[int, float]] = {}
        self._tasks: set[asyncio.Task] = set()
        publisher.add_listener(self._on_sent)
        state.subscribe(self._on_state_event)

    # ------------------------------------------------------------------ helpers

    def _now(self) -> float:
        return self.clock.now().timestamp()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Wait for background tasks (verification, corrections). For tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _on_sent(self, cmd: SentCommand) -> None:
        if cmd.target == ALL_LAMPS_GROUP:
            names = [d.friendly_name for d in self.config.devices.values() if d.in_all_off]
        elif cmd.target in self.config.devices:
            names = [cmd.target]
        else:
            room = next((r.room for r in self.config.rooms.values() if r.z2m_group == cmd.target), None)
            names = [d.friendly_name for d in self.config.lamps(room)] if room else []
        for name in names:
            self._last_cmd_at[name] = cmd.at
        if "color_temp" in cmd.payload and cmd.target in self.config.devices:
            self._last_colour[cmd.target] = (cmd.payload["color_temp"], cmd.at)

    def is_on(self, name: str) -> bool:
        return self.state.get(name).is_on

    def room_is_on(self, room: str, *, ignore_locked: bool = False) -> bool:
        lamps = self.config.lamps(room)
        if ignore_locked:
            lamps = self._unlocked(lamps)
        return any(self.is_on(d.friendly_name) for d in lamps)

    def _unlocked(self, lamps: list[Device]) -> list[Device]:
        return [d for d in lamps if d.friendly_name not in self.locked]

    async def _turn_on(self, lamp: Device, brightness: int | None, transition: float, mired: int) -> bool:
        if not lamp.dimmable:
            return await self.publisher.send(lamp.friendly_name, {"state": "ON"})
        payload = {"state": "ON", "brightness": brightness or 254, "transition": transition}
        if lamp.has_colour:
            payload["color_temp"] = mired
        return await self.publisher.send(lamp.friendly_name, payload)

    async def _turn_off(self, lamp: Device, transition: float) -> bool:
        payload = {"state": "OFF"}
        if lamp.dimmable:
            payload["transition"] = transition
        return await self.publisher.send(lamp.friendly_name, payload)

    # ------------------------------------------------------------------ scenes / rooms

    async def apply_scene(self, room: str, scene: str, *, transition: float, explicit: bool) -> None:
        """Apply `scene` fully to the room's unlocked lamps (on/off and brightness), f = 1."""
        mired = self._mired()
        targets = scene_targets(self.config, room, scene)
        for lamp in self._unlocked(self.config.lamps(room)):
            target = targets[lamp.friendly_name]
            current = self.state.get(lamp.friendly_name).state
            if target.on:
                if not lamp.dimmable and current == "ON" and not explicit:
                    continue
                await self._turn_on(lamp, target.brightness, transition, mired)
            elif explicit or current != "OFF":
                await self._turn_off(lamp, transition)
        self.store.save_room_light(room, RoomLight(scene, 1.0, scene_bases(self.config, room, scene)))

    async def room_on(self, room: str) -> None:
        """Top button: (re)apply the current phase scene, f = 1."""
        await self.apply_scene(room, self._phase(), transition=T_BUTTON, explicit=True)

    async def room_off(self, room: str) -> None:
        """Bottom button: all lamps of the room with in_room_off = yes."""
        for lamp in self._unlocked(self.config.lamps(room)):
            if lamp.in_room_off:
                await self._turn_off(lamp, T_BUTTON)

    async def dim(self, room: str, direction: str) -> bool:
        light = self.store.room_light(room)
        lamps = [d for d in self._unlocked(self.config.lamps(room)) if d.dimmable and self.is_on(d.friendly_name)]
        if not lamps:
            return False
        bases = {}
        for lamp in lamps:
            base = light.bases.get(lamp.friendly_name)
            if base is None:  # lamp turned on outside a scene: derive from what it shows now
                current = self.state.get(lamp.friendly_name).brightness or 1
                base = current / light.factor
            bases[lamp.friendly_name] = base
        factor = next_factor(list(bases.values()), light.factor, direction)
        if factor is None:
            log.info("Dim %s %s: at limit", room, direction)
            return False
        for lamp in lamps:
            await self.publisher.send(
                lamp.friendly_name, {"brightness": level(bases[lamp.friendly_name], factor), "transition": T_DIM}
            )
        light.factor = factor
        light.bases.update(bases)
        self.store.save_room_light(room, light)
        return True

    async def on_phase_change(self, phase: str) -> None:
        """Rooms that are on fade to the new scene; rooms that are off stay off.

        A room lit only by a running sunrise counts as off here: otherwise a
        phase boundary during the animation would switch on the room's other lamps.
        """
        for room in self.config.rooms:
            if self.room_is_on(room, ignore_locked=True):
                log.info("Phase %s: fading %s", phase, room)
                await self.apply_scene(room, phase, transition=T_PHASE, explicit=False)
            else:
                self.store.save_room_light(room, RoomLight(phase, 1.0, scene_bases(self.config, room, phase)))

    # ------------------------------------------------------------------ whole apartment

    async def all_on(self) -> None:
        for room in self.config.rooms:
            await self.room_on(room)

    async def all_off(self, verify: bool = True) -> None:
        lamps = [d for d in self.config.devices.values() if d.in_all_off and not d.protected]
        if self.locked:
            for lamp in self._unlocked(lamps):
                await self._turn_off(lamp, T_BUTTON)
        else:
            await self.publisher.send(ALL_LAMPS_GROUP, {"state": "OFF", "transition": T_BUTTON})
        if verify:
            self._spawn(self.verify({d.friendly_name: False for d in self._unlocked(lamps)}))

    async def verify_critical(
        self, expected_on: dict[str, bool], delay: float = VERIFY_DELAY_S
    ) -> list[str]:
        """Verify, resend, verify again. Returns lamps that still did not follow.

        For critical commands (sunrise): checks locked lamps too.
        """
        if self.publisher.dry_run:
            return []
        await self.verify(expected_on, delay, include_locked=True)
        await self.clock.sleep(delay)
        return [name for name, want_on in expected_on.items() if self.is_on(name) != want_on]

    async def verify(
        self, expected_on: dict[str, bool], delay: float = VERIFY_DELAY_S, *, include_locked: bool = False
    ) -> list[str]:
        """After `delay`, resend individually to lamps whose reported state differs.

        Group commands are unacknowledged; this catches lamps that missed one.
        Returns the lamps that needed a resend.
        """
        if self.publisher.dry_run:
            return []  # nothing really changes in dry run, so nothing to verify
        await self.clock.sleep(delay)
        mired = self._mired()
        resent = []
        for name, want_on in expected_on.items():
            if (name in self.locked and not include_locked) or self.is_on(name) == want_on:
                continue
            lamp = self.config.devices[name]
            log.warning("Verify: %s did not follow (wanted %s) - resending", name, "ON" if want_on else "OFF")
            if want_on:
                await self._turn_on(lamp, self.state.get(name).brightness, T_BUTTON, mired)
            else:
                await self._turn_off(lamp, T_BUTTON)
            resent.append(name)
        return resent

    # ------------------------------------------------------------------ single lamp (app)

    async def set_lamp(self, name: str, on: bool | None = None, brightness: int | None = None) -> bool:
        lamp = self.config.devices.get(name)
        if lamp is None or not lamp.is_lamp or name in self.locked:
            return False
        if on is False:
            return await self._turn_off(lamp, T_BUTTON)
        light = self.store.room_light(lamp.room)
        if lamp.dimmable:
            if brightness is None:
                base = light.bases.get(name)
                brightness = level(base, light.factor) if base else (self.state.get(name).brightness or 254)
            brightness = max(1, min(254, int(brightness)))
            light.bases[name] = brightness / light.factor
            self.store.save_room_light(lamp.room, light)
        return await self._turn_on(lamp, brightness, T_BUTTON, self._mired())

    # ------------------------------------------------------------------ colour

    async def sync_colour(self, transition: float = T_COLOUR) -> list[str]:
        """Bring every lit, unlocked colour lamp to the current colour.

        Compares with the colour each lamp *reports*, so it also fixes lamps
        that missed an update or came on with an old colour.
        """
        mired = self._mired()
        now = self._now()
        sent = []
        for lamp in self._unlocked(self.config.lamps()):
            if not lamp.has_colour or not self.is_on(lamp.friendly_name):
                continue
            reported = self.state.get(lamp.friendly_name).color_temp
            if reported is not None and abs(reported - mired) <= COLOUR_TOLERANCE:
                continue
            last = self._last_colour.get(lamp.friendly_name)
            if last and last[0] == mired and now - last[1] < COLOUR_RESEND_S:
                continue  # already asked for this colour recently
            if await self.publisher.send(lamp.friendly_name, {"color_temp": mired, "transition": transition}):
                sent.append(lamp.friendly_name)
        return sent

    def _on_state_event(self, event) -> None:
        if not isinstance(event, StateChanged) or "state" not in event.changes:
            return
        old, new = event.changes["state"]
        if old != "OFF" or new != "ON":
            return
        lamp = self.config.devices.get(event.name)
        if lamp is None or not lamp.is_lamp or not lamp.has_colour or event.name in self.locked:
            return
        if self._now() - self._last_cmd_at.get(event.name, 0.0) < EXTERNAL_ON_GRACE_S:
            return  # we switched it on ourselves
        log.info("%s was switched on outside the app - setting current colour", event.name)
        self._spawn(self.publisher.send(event.name, {"color_temp": self._mired(), "transition": T_FIX}))
