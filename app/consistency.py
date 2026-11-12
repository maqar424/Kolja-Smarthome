"""Startup check: does the committed config still match the live Zigbee2MQTT?

Runs once bridge/devices and bridge/groups have arrived (they are retained).
Only logs warnings; the app keeps running. The import script runs the same
kind of checks against docs/z2m-snapshot.json when the sheet is converted.
"""

from __future__ import annotations

import logging

from app.config import ALL_LAMPS_GROUP, COMMON_MIRED, AppConfig
from app.z2m import capabilities_from_exposes

log = logging.getLogger(__name__)


def check_consistency(config: AppConfig, z2m_devices: list[dict], z2m_groups: list[dict]) -> list[str]:
    warnings: list[str] = []
    live = {d.get("friendly_name"): d for d in z2m_devices if d.get("type") != "Coordinator"}

    for name, device in config.devices.items():
        z2m = live.get(name)
        if z2m is None:
            warnings.append(f"{name}: in config but not known to Zigbee2MQTT")
            continue
        definition = z2m.get("definition") or {}
        exposes = definition.get("exposes") if definition else z2m.get("exposes")
        if exposes is None:
            continue  # device not interviewed yet
        caps = capabilities_from_exposes(exposes)
        if device.is_lamp and caps["brightness"] != device.dimmable:
            warnings.append(f"{name}: config dimmable={device.dimmable}, Z2M says {caps['brightness']}")
        ct = caps["color_temp"]
        if device.has_colour and ct and not (ct[0] <= COMMON_MIRED[0] and ct[1] >= COMMON_MIRED[1]):
            warnings.append(f"{name}: Z2M colour range {ct} does not cover {list(COMMON_MIRED)}")

    groups = {g.get("friendly_name"): g for g in z2m_groups}
    all_lamps = groups.get(ALL_LAMPS_GROUP)
    if all_lamps is None:
        warnings.append(f"Z2M group {ALL_LAMPS_GROUP} missing")
    else:
        members = {m.get("friendly_name") for m in all_lamps.get("members", [])}
        flagged = {d.friendly_name for d in config.devices.values() if d.in_all_off}
        for name in sorted(flagged - members):
            warnings.append(f"{name}: in_all_off = yes but not in Z2M group {ALL_LAMPS_GROUP}")
        for name in sorted(members - flagged):
            warnings.append(f"{name}: in Z2M group {ALL_LAMPS_GROUP} but in_all_off = no")
        protected = {d.friendly_name for d in config.devices.values() if d.protected}
        for name in sorted(members & protected):
            warnings.append(f"{name}: PROTECTED device is in {ALL_LAMPS_GROUP} - 'Alles aus' would switch it!")

    for room in config.rooms.values():
        if room.z2m_group not in groups:
            warnings.append(f"Room {room.room}: Z2M group {room.z2m_group} missing")

    for msg in warnings:
        log.warning("Config check: %s", msg)
    if not warnings:
        log.info("Config check: config matches Zigbee2MQTT")
    return warnings
