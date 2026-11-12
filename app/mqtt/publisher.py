"""The single chokepoint for every MQTT command the app sends.

Every command, whether from a wall switch, the scheduler, the sunrise or the
web UI, goes through `Publisher.send()`. The hard rules from CLAUDE.md are
enforced *here*, in one place, so a bug elsewhere cannot break them:

1. Nothing is ever sent to a protected device (pveSwitch powers the Proxmox host).
2. Only known targets: lamps/appliances from the config, room groups, Alle_Lampen.
   Remotes, unknown names and bridge topics are refused.
3. Only the payload keys state/brightness/color_temp/transition. That rules out
   scene_recall/scene_store, per-lamp colour (x/y/hue), effects and so on.
4. On/off-only devices (plugs) never receive brightness or colour.
5. Groups only receive "off" (all-off / room-off). Colour and brightness are
   always sent per lamp, because the groups contain plugs.
6. color_temp is clamped to the common range 153-370, brightness to 1-254.
7. DRY_RUN: the command is logged and recorded, but not published.

A refused command is logged as an ERROR and `send()` returns False. It never
raises, so a refused command can't crash a button handler.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.clock import Clock
from app.config import COMMON_MIRED, AppConfig

log = logging.getLogger(__name__)

Transport = Callable[[str, str], Awaitable[None]]  # (topic, payload json) -> None

ALLOWED_KEYS = {"state", "brightness", "color_temp", "transition"}
GROUP_ALLOWED_KEYS = {"state", "transition"}


@dataclass(frozen=True)
class SentCommand:
    at: float  # unix timestamp
    target: str
    topic: str
    payload: dict
    dry_run: bool


class Publisher:
    def __init__(
        self,
        config: AppConfig,
        clock: Clock,
        *,
        base_topic: str,
        dry_run: bool,
        transport: Transport | None = None,
        allow_reads_in_dry_run: bool = False,
    ) -> None:
        self._config = config
        self._clock = clock
        self._base = base_topic
        self.dry_run = dry_run
        self.allow_reads_in_dry_run = allow_reads_in_dry_run
        self._transport = transport
        self.history: deque[SentCommand] = deque(maxlen=200)
        self.refused_count = 0
        self._listeners: list[Callable[[SentCommand], None]] = []

    def set_transport(self, transport: Transport | None) -> None:
        self._transport = transport

    def add_listener(self, listener: Callable[[SentCommand], None]) -> None:
        """Called for every accepted command right before it is published (also in dry run)."""
        self._listeners.append(listener)

    # ------------------------------------------------------------------ public

    async def send(self, target: str, payload: dict) -> bool:
        clean = self._check(target, payload)
        if clean is None:
            self.refused_count += 1
            return False

        topic = f"{self._base}/{target}/set"
        text = json.dumps(clean, separators=(",", ":"))
        cmd = SentCommand(self._clock.now().timestamp(), target, topic, clean, self.dry_run)
        # Tell listeners *before* publishing: Z2M's state reply can arrive before
        # publish() returns, and the light service must already know the
        # resulting ON was ours (not "switched on outside the app").
        for listener in self._listeners:
            try:
                listener(cmd)
            except Exception:  # noqa: BLE001
                log.exception("Publisher listener failed")

        if self.dry_run:
            log.info("DRY RUN  %s  %s", topic, text)
        else:
            if self._transport is None:
                log.error("Not connected to MQTT - dropped %s %s", topic, text)
                return False
            try:
                await self._transport(topic, text)
            except Exception:  # noqa: BLE001 - never let a broker hiccup crash a caller
                log.exception("Publish failed: %s %s", topic, text)
                return False
            log.info("SEND     %s  %s", topic, text)

        self.history.append(cmd)
        return True

    async def request_state(self, target: str) -> bool:
        """Ask Z2M to read a device's current state (`<name>/get`).

        Read-only, but still a message to the device, so: never for protected
        devices, only for lamps/appliances, and in DRY_RUN only when
        `allow_reads_in_dry_run` is set.
        """
        device = self._config.devices.get(target)
        if device is None or device.protected or device.category not in ("light", "appliance") or not device.on_off:
            self._refuse(target, {"get": "state"}, "state read not allowed for this target")
            return False
        topic = f"{self._base}/{target}/get"
        if self.dry_run and not self.allow_reads_in_dry_run:
            log.info("DRY RUN  %s  (state read skipped)", topic)
            return True
        if self._transport is None:
            return False
        try:
            await self._transport(topic, '{"state":""}')
        except Exception:  # noqa: BLE001
            log.exception("State read failed: %s", topic)
            return False
        log.debug("GET      %s", topic)
        return True

    # ------------------------------------------------------------------ guards

    def _refuse(self, target: str, payload: dict, reason: str) -> None:
        log.error("REFUSED  %s %s: %s", target, payload, reason)

    def _check(self, target: str, payload: dict) -> dict | None:
        if not isinstance(payload, dict) or not payload:
            self._refuse(target, payload, "empty or invalid payload")
            return None

        unknown = set(payload) - ALLOWED_KEYS
        if unknown:
            self._refuse(target, payload, f"payload keys not allowed: {sorted(unknown)}")
            return None

        device = self._config.devices.get(target)
        is_group = target in self._config.group_names()

        if device is not None and device.protected:
            self._refuse(target, payload, "protected device - never send commands to it")
            return None
        if device is None and not is_group:
            self._refuse(target, payload, "unknown target")
            return None
        if device is not None and device.category not in ("light", "appliance"):
            self._refuse(target, payload, f"category {device.category} cannot be commanded")
            return None

        clean: dict = {}
        if "state" in payload:
            state = str(payload["state"]).upper()
            if state not in ("ON", "OFF"):
                self._refuse(target, payload, "state must be ON or OFF")
                return None
            clean["state"] = state

        if is_group:
            if set(payload) - GROUP_ALLOWED_KEYS or clean.get("state") != "OFF":
                self._refuse(target, payload, "groups only accept state OFF (colour/brightness go per lamp)")
                return None
        else:
            wants_light = "brightness" in payload or "color_temp" in payload
            if wants_light and not device.dimmable:
                self._refuse(target, payload, "on/off-only device: no brightness or colour")
                return None
            if "brightness" in payload:
                clean["brightness"] = _clamp_int(payload["brightness"], 1, 254)
                if clean["brightness"] is None:
                    self._refuse(target, payload, "brightness is not a number")
                    return None
            if "color_temp" in payload:
                if device.color_temp_range is None:
                    self._refuse(target, payload, "device has no colour temperature")
                    return None
                clean["color_temp"] = _clamp_int(payload["color_temp"], *COMMON_MIRED)
                if clean["color_temp"] is None:
                    self._refuse(target, payload, "color_temp is not a number")
                    return None

        if "transition" in payload:
            try:
                transition = round(max(0.0, min(float(payload["transition"]), 60.0)), 1)
            except (TypeError, ValueError):
                self._refuse(target, payload, "transition is not a number")
                return None
            clean["transition"] = int(transition) if transition.is_integer() else transition

        if not set(clean) - {"transition"}:
            self._refuse(target, payload, "nothing to send")
            return None
        return clean


def _clamp_int(value, low: int, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))
