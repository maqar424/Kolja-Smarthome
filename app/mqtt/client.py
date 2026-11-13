"""MQTT connection: subscribe to everything under the Z2M base topic, feed the
state cache, give the publisher a way to send, and reconnect forever.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import aiomqtt

from app.config import AppConfig, Settings
from app.consistency import check_consistency
from app.mqtt.publisher import Publisher
from app.state import StateStore

log = logging.getLogger(__name__)


def client_id() -> str:
    """Unique per machine/container. The broker drops a connection when a second
    one arrives with the same id, so a fixed id would make the Pi app and a
    dev instance on the VM kick each other off in a loop."""
    return f"kolja-smarthome-{socket.gethostname()}"[:64]


class MqttClient:
    def __init__(self, settings: Settings, config: AppConfig, state: StateStore, publisher: Publisher) -> None:
        self._settings = settings
        self._config = config
        self._state = state
        self._publisher = publisher
        self.connected = False
        self.consistency_warnings: list[str] | None = None

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._settings.mqtt_host,
                    port=self._settings.mqtt_port,
                    identifier=client_id(),
                    keepalive=30,
                ) as client:
                    await self._on_connect(client)
                    backoff = 1.0
                    async for message in client.messages:
                        self._state.handle_message(str(message.topic), message.payload or b"")
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection to %s:%s lost/failed: %s - retry in %.0f s",
                            self._settings.mqtt_host, self._settings.mqtt_port, exc, backoff)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the connection loop alive whatever happens
                log.exception("Unexpected error in MQTT loop - retry in %.0f s", backoff)
            finally:
                self.connected = False
                self._publisher.set_transport(None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _on_connect(self, client: aiomqtt.Client) -> None:
        async def transport(topic: str, payload: str) -> None:
            await client.publish(topic, payload, qos=1)

        await client.subscribe(f"{self._settings.base_topic}/#", qos=1)
        self._publisher.set_transport(transport)
        self.connected = True
        log.info("Connected to MQTT %s:%s (dry run: %s)",
                 self._settings.mqtt_host, self._settings.mqtt_port, self._publisher.dry_run)
        asyncio.create_task(self._after_connect(), name="mqtt-after-connect")

    async def _after_connect(self) -> None:
        # bridge/devices and bridge/groups are retained, so they arrive right after subscribing.
        for _ in range(50):
            if self._state.z2m_devices is not None and self._state.z2m_groups is not None:
                break
            await asyncio.sleep(0.1)
        if self.consistency_warnings is None and self._state.z2m_devices is not None:
            self.consistency_warnings = check_consistency(
                self._config, self._state.z2m_devices, self._state.z2m_groups or []
            )
        # Lamp states are not retained: ask each lamp once, gently spaced.
        for device in self._config.devices.values():
            if device.is_lamp and self.connected:
                await self._publisher.request_state(device.friendly_name)
                await asyncio.sleep(0.1)
