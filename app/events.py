"""Tiny change broadcaster for the live UI stream (Server-Sent Events).

Anything that may change what the UI shows calls `poke()` (lamp state
updates, API actions, phase changes). Each connected browser waits on its
own asyncio.Event; a poke wakes all of them, and each sends a fresh snapshot.
"""

from __future__ import annotations

import asyncio


class Changes:
    def __init__(self) -> None:
        self._listeners: set[asyncio.Event] = set()

    def poke(self, *_args) -> None:
        for event in list(self._listeners):
            event.set()

    def listen(self) -> asyncio.Event:
        event = asyncio.Event()
        self._listeners.add(event)
        return event

    def unlisten(self, event: asyncio.Event) -> None:
        self._listeners.discard(event)

    @property
    def listeners(self) -> int:
        return len(self._listeners)
