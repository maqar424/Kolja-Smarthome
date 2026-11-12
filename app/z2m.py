"""Helpers for Zigbee2MQTT data structures (shared by the app and tools/import_sheet.py)."""

from __future__ import annotations

from typing import Any


def group_member_names(group: dict, devices: list[dict]) -> set[str]:
    """Friendly names of a group's members.

    Live `bridge/groups` lists members as {"ieee_address", "endpoint"} only;
    the trimmed snapshot has {"friendly_name", "endpoint"}. Handle both, using
    `bridge/devices` to map addresses to names.
    """
    by_ieee = {d.get("ieee_address"): d.get("friendly_name") for d in devices}
    names = set()
    for member in group.get("members", []):
        name = member.get("friendly_name") or by_ieee.get(member.get("ieee_address"))
        names.add(name or f"<unknown {member.get('ieee_address')}>")
    return names


def capabilities_from_exposes(exposes: list[dict]) -> dict[str, Any]:
    """Summarise Z2M `exposes` into what the app needs to know.

    {"on_off": bool, "brightness": bool, "color_temp": [min, max] | None,
     "power": bool, "actions": [..] | None}
    """
    props: dict[str, dict] = {}

    def walk(items: list[dict]) -> None:
        for item in items:
            if "features" in item:
                walk(item["features"])
            elif item.get("property"):
                props.setdefault(item["property"], item)

    walk(exposes or [])
    caps: dict[str, Any] = {
        "on_off": "state" in props,
        "brightness": "brightness" in props and "state" in props,
        "color_temp": None,
        "power": "power" in props,
        "actions": None,
    }
    ct = props.get("color_temp")
    if ct and "state" in props:
        caps["color_temp"] = [ct.get("value_min"), ct.get("value_max")]
    action = props.get("action")
    if action and action.get("values"):
        caps["actions"] = list(action["values"])
    return caps
