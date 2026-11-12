"""A tiny fake Zigbee2MQTT for integration tests.

Publishes retained bridge/state, bridge/devices and bridge/groups from
docs/z2m-snapshot.json, answers `<name>/set` and `<name>/get` with state
updates like the real thing, and can simulate trouble:
  - `drop`: devices that silently ignore commands (unreachable lamp)
  - `press(name, action)`: a button press
"""

from __future__ import annotations

import asyncio
import json
import time

import aiomqtt


class FakeZ2M:
    def __init__(self, snapshot: dict, host: str, port: int, base_topic: str) -> None:
        self.snapshot = snapshot
        self.host, self.port, self.base = host, port, base_topic
        self.devices = {d["friendly_name"]: d for d in snapshot["devices"]}
        self.groups = {g["friendly_name"]: g for g in snapshot["groups"]}
        self.states: dict[str, dict] = {}
        self.received: list[tuple[str, dict]] = []  # every /set and /get, in order
        self.drop: set[str] = set()
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    async def __aenter__(self) -> "FakeZ2M":
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), 5)
        return self

    async def __aexit__(self, *exc) -> None:
        # clear retained messages so the next test starts clean
        for sub in ("bridge/state", "bridge/devices", "bridge/groups"):
            await self._client.publish(f"{self.base}/{sub}", b"", retain=True)
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def set_state(self, name: str, **values) -> None:
        self.states.setdefault(name, {"state": "OFF"}).update(values)

    async def publish_state(self, name: str) -> None:
        await self._client.publish(f"{self.base}/{name}", json.dumps(self.states.get(name, {"state": "OFF"})))

    async def press(self, name: str, action: str) -> None:
        await self._client.publish(f"{self.base}/{name}", json.dumps({"action": action, "battery": 100}))

    async def _run(self) -> None:
        async with aiomqtt.Client(self.host, self.port, identifier=f"fake-z2m-{time.time_ns()}") as client:
            self._client = client
            await client.subscribe(f"{self.base}/+/set")
            await client.subscribe(f"{self.base}/+/get")
            pub = client.publish
            await pub(f"{self.base}/bridge/state", json.dumps({"state": "online"}), retain=True)
            live_devices = [
                {**{k: v for k, v in d.items() if k != "exposes"}, "definition": {"exposes": d["exposes"]}}
                for d in self.snapshot["devices"]
            ]
            await pub(f"{self.base}/bridge/devices", json.dumps(live_devices), retain=True)
            await pub(f"{self.base}/bridge/groups", json.dumps(self.snapshot["groups"]), retain=True)
            self._ready.set()
            async for message in client.messages:
                await self._handle(str(message.topic), message.payload)

    async def _handle(self, topic: str, payload: bytes) -> None:
        _, name, verb = topic.split("/")
        data = json.loads(payload or b"{}")
        self.received.append((f"{name}/{verb}", data))
        if verb == "get":
            if name in self.devices and name not in self.drop:
                await self.publish_state(name)
            return
        targets = [m["friendly_name"] for m in self.groups[name]["members"]] if name in self.groups else [name]
        for target in targets:
            if target in self.drop or target not in self.devices:
                continue
            update = {k: v for k, v in data.items() if k in ("state", "brightness", "color_temp")}
            self.set_state(target, **update)
            await self.publish_state(target)
