#!/usr/bin/env python3
"""Convert Kolja's master sheet (docs/devices.xlsx) into versioned config files.

    python tools/import_sheet.py            # writes config/*.json
    python tools/import_sheet.py --check    # only validate, write nothing

Why a script and not "the app reads Excel"? The running app must not depend on
Excel or openpyxl. The sheet stays the human-facing master; this script turns
it into plain JSON that is committed, so every change is visible in a git diff.

Columns are looked up by header name, so adding or reordering columns in Excel
does not break the import. Comment columns (your_comment, claude_note) and
volatile facts (battery_pct) are not exported.

Validation:
  errors   -> the sheet is inconsistent with itself; nothing is written
  warnings -> the sheet disagrees with the Zigbee2MQTT snapshot; files are
              written, but look at them (the app logs the same checks at
              startup against the live Z2M data)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = REPO_ROOT / "docs" / "devices.xlsx"
DEFAULT_SNAPSHOT = REPO_ROOT / "docs" / "z2m-snapshot.json"
DEFAULT_SVG = REPO_ROOT / "docs" / "apartment-floorplan.svg"
DEFAULT_OUT = REPO_ROOT / "config"

ALL_LAMPS_GROUP = "Alle_Lampen"
# Hard rule: this plug powers the Proxmox host. It may be read, never commanded.
PROTECTED_DEVICES = {"pveSwitch"}
# Every colour lamp must support this common range (requirements §4).
COMMON_MIRED = (153, 370)

SHAPES = {"point", "line", "rect"}
MOUNTS = {"floor", "middle", "ceiling", "vertical"}
CATEGORIES = {"light", "appliance", "remote"}
PHASE_SCENES = ("kalt", "warm", "twilight", "night")


# --------------------------------------------------------------------------- reading


def read_workbook(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Return {sheet name: [row dict keyed by header]} for the data sheets.

    A sheet's table ends at the first completely empty row, so notes below the
    table (as in SceneBrightness) are ignored.
    """
    import openpyxl  # dev-only dependency, imported lazily

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets: dict[str, list[dict[str, Any]]] = {}
    for name in ("Devices", "Rooms", "Scenes", "SceneBrightness"):
        if name not in wb.sheetnames:
            raise SystemExit(f"Sheet '{name}' missing in {path}")
        rows = wb[name].iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
        table = []
        for row in rows:
            if all(v is None or str(v).strip() == "" for v in row):
                break
            table.append({h: _clean(v) for h, v in zip(header, row) if h})
        sheets[name] = table
    return sheets


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------- helpers


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _yes(value: Any, where: str, report: Report) -> bool:
    text = str(value or "").strip().lower()
    if text in ("yes", "ja"):
        return True
    if text in ("no", "nein", ""):
        return False
    report.error(f"{where}: expected yes/no, got {value!r}")
    return False


def parse_geometry(shape: str, text: str) -> list:
    """Parse geometry_cm into a list of parts (several parts separated by ';').

    point -> [[x, y], ...]            one point per part
    line  -> [[[x, y], [x, y], ...]]  one polyline per part
    rect  -> [[x, y, w, h], ...]      one rect per part
    Raises ValueError on malformed input.
    """
    parts = [p.strip() for p in str(text).split(";") if p.strip()]
    if not parts:
        raise ValueError("empty geometry")
    out: list = []
    for part in parts:
        if shape == "point":
            nums = _numbers(part, 2)
            out.append(nums)
        elif shape == "rect":
            nums = _numbers(part, 4)
            if nums[2] <= 0 or nums[3] <= 0:
                raise ValueError(f"rect needs positive width/height: {part!r}")
            out.append(nums)
        elif shape == "line":
            points = [_numbers(p, 2) for p in part.split()]
            if len(points) < 2:
                raise ValueError(f"line needs at least two points: {part!r}")
            out.append(points)
        else:
            raise ValueError(f"unknown shape {shape!r}")
    return out


def _numbers(text: str, count: int) -> list[float]:
    items = [t for t in re.split(r"\s*,\s*", text.strip()) if t]
    if len(items) != count:
        raise ValueError(f"expected {count} numbers in {text!r}")
    nums = [float(t) for t in items]
    return [int(n) if n.is_integer() else n for n in nums]


def geometry_points(shape: str, geometry: list) -> list[list[float]]:
    """All points that must lie inside the room (rect -> its four corners)."""
    if shape == "point":
        return list(geometry)
    if shape == "line":
        return [p for line in geometry for p in line]
    return [
        corner
        for x, y, w, h in geometry
        for corner in ([x, y], [x + w, y], [x, y + h], [x + w, y + h])
    ]


def parse_height(text: Any) -> list[int] | None:
    if text is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(text))
    if not match:
        raise ValueError(f"height_cm must look like '0-180', got {text!r}")
    low, high = int(match.group(1)), int(match.group(2))
    if low >= high:
        raise ValueError(f"height_cm range is empty: {text!r}")
    return [low, high]


def svg_room_polygons(svg_text: str) -> dict[str, list[tuple[float, float]]]:
    """Polygons of the room paths (ids) in the floor plan. Only M/L/Z paths."""
    polygons = {}
    for match in re.finditer(r'<path\s+id="([^"]+)"[^>]*\sd="([^"]+)"', svg_text):
        path_id, d = match.groups()
        coords = re.findall(r"[ML]\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", d)
        polygons[path_id] = [(float(x), float(y)) for x, y in coords]
    return polygons


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting; points on the boundary count as inside."""
    n = len(polygon)
    inside = False
    for i in range(n):
        (x1, y1), (x2, y2) = polygon[i], polygon[(i + 1) % n]
        # on an edge?
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) < 1e-9 and min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2):
            return True
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def capabilities_from_exposes(exposes: list[dict]) -> dict:
    """Summarise Z2M `exposes` into what the app needs to know."""
    props: dict[str, dict] = {}

    def walk(items: list[dict]) -> None:
        for item in items:
            if "features" in item:
                walk(item["features"])
            elif item.get("property"):
                props.setdefault(item["property"], item)

    walk(exposes)
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


# --------------------------------------------------------------------------- building


def build_config(
    sheets: dict[str, list[dict[str, Any]]],
    snapshot: dict,
    svg_text: str,
) -> tuple[dict[str, Any], Report]:
    report = Report()
    polygons = svg_room_polygons(svg_text)
    z2m_devices = {d["friendly_name"]: d for d in snapshot.get("devices", [])}
    z2m_groups = {g["friendly_name"]: g for g in snapshot.get("groups", [])}

    rooms = _build_rooms(sheets["Rooms"], polygons, z2m_groups, report)
    room_by_name = {r["room"]: r for r in rooms}
    devices = _build_devices(sheets["Devices"], room_by_name, polygons, z2m_devices, report)
    scenes = _build_scenes(sheets["Scenes"], sheets["SceneBrightness"], devices, report)
    _check_groups(devices, rooms, z2m_groups, report)

    config = {
        "rooms.json": {"rooms": rooms},
        "devices.json": {"devices": devices},
        "scenes.json": scenes,
    }
    return config, report


def _build_rooms(rows, polygons, z2m_groups, report: Report) -> list[dict]:
    rooms = []
    seen = set()
    for i, row in enumerate(rows, start=2):
        where = f"Rooms row {i}"
        name = row.get("room")
        if not name:
            report.error(f"{where}: room name missing")
            continue
        if name in seen:
            report.error(f"{where}: duplicate room {name!r}")
        seen.add(name)
        svg_id = row.get("svg_id")
        if svg_id not in polygons:
            report.error(f"{where}: svg_id {svg_id!r} is not a path id in the floor plan")
        group = row.get("z2m_group")
        group_id = row.get("z2m_group_id")
        z2m = z2m_groups.get(group)
        if z2m is None:
            report.warn(f"{where}: Z2M group {group!r} not in snapshot")
        elif z2m["id"] != group_id:
            report.warn(f"{where}: group {group!r} has id {z2m['id']} in Z2M, sheet says {group_id}")
        rooms.append(
            {
                "room": name,
                "display_name": row.get("display_name") or name,
                "sort_order": row.get("sort_order") or 0,
                "z2m_group": group,
                "z2m_group_id": group_id,
                "svg_id": svg_id,
            }
        )
    rooms.sort(key=lambda r: r["sort_order"])
    return rooms


def _build_devices(rows, room_by_name, polygons, z2m_devices, report: Report) -> list[dict]:
    devices = []
    seen = set()
    for i, row in enumerate(rows, start=2):
        name = row.get("friendly_name")
        where = f"Devices row {i} ({name})"
        if not name:
            report.error(f"Devices row {i}: friendly_name missing")
            continue
        if name in seen:
            report.error(f"{where}: duplicate friendly_name")
        seen.add(name)

        category = row.get("category")
        if category not in CATEGORIES:
            report.error(f"{where}: category must be one of {sorted(CATEGORIES)}, got {category!r}")
        room = row.get("room")
        if room is not None and room not in room_by_name:
            report.error(f"{where}: room {room!r} not in sheet Rooms")

        device: dict[str, Any] = {
            "friendly_name": name,
            "room": room,
            "display_name": row.get("display_name") or name,
            "category": category,
            "in_app": _yes(row.get("in_app"), f"{where} in_app", report),
            "in_room_off": _yes(row.get("in_room_off"), f"{where} in_room_off", report),
            "in_all_off": _yes(row.get("in_all_off"), f"{where} in_all_off", report),
            "sort_order": row.get("sort_order") or 0,
            "protected": name in PROTECTED_DEVICES,
            "model": row.get("model"),
        }

        if device["protected"] and (device["in_app"] or device["in_room_off"] or device["in_all_off"]):
            report.error(f"{where}: protected device must have in_app/in_room_off/in_all_off = no")

        # Position on the floor plan
        shape, geometry_text = row.get("shape"), row.get("geometry_cm")
        device["shape"] = shape
        device["geometry"] = None
        device["mount"] = row.get("mount")
        device["height_cm"] = None
        if shape or geometry_text:
            _parse_position(row, device, room_by_name, polygons, where, report)
        elif device["in_app"] and category == "light":
            report.error(f"{where}: light shown in the app needs shape and geometry_cm")

        # Capabilities from Z2M
        z2m = z2m_devices.get(name)
        if z2m is None:
            report.warn(f"{where}: not found in Z2M snapshot")
            device["capabilities"] = None
        else:
            caps = capabilities_from_exposes(z2m.get("exposes", []))
            device["capabilities"] = caps
            device["model"] = z2m.get("model") or device["model"]
            if category == "light" and not caps["on_off"]:
                report.error(f"{where}: category light but Z2M exposes no on/off state")
            ct = caps["color_temp"]
            if ct and not (ct[0] <= COMMON_MIRED[0] and ct[1] >= COMMON_MIRED[1]):
                report.error(f"{where}: colour range {ct} does not cover the common range {list(COMMON_MIRED)}")
            if caps["brightness"] and not ct:
                report.warn(f"{where}: dimmable but without colour temperature")
        devices.append(device)

    devices.sort(key=lambda d: (_room_sort(d, room_by_name), d["sort_order"], d["friendly_name"]))
    return devices


def _room_sort(device, room_by_name) -> int:
    room = room_by_name.get(device["room"])
    return room["sort_order"] if room else 10_000


def _parse_position(row, device, room_by_name, polygons, where, report: Report) -> None:
    shape = device["shape"]
    if shape not in SHAPES:
        report.error(f"{where}: shape must be one of {sorted(SHAPES)}, got {shape!r}")
        return
    try:
        geometry = parse_geometry(shape, row.get("geometry_cm") or "")
    except ValueError as exc:
        report.error(f"{where}: geometry_cm: {exc}")
        return
    device["geometry"] = geometry

    mount = device["mount"]
    if mount not in MOUNTS:
        report.error(f"{where}: mount must be one of {sorted(MOUNTS)}, got {mount!r}")
    try:
        device["height_cm"] = parse_height(row.get("height_cm"))
    except ValueError as exc:
        report.error(f"{where}: {exc}")
    if mount == "vertical" and device["height_cm"] is None:
        report.error(f"{where}: mount vertical needs height_cm like '0-180'")

    room = room_by_name.get(device["room"])
    polygon = polygons.get(room["svg_id"]) if room else None
    if polygon:
        outside = [p for p in geometry_points(shape, geometry) if not point_in_polygon(p[0], p[1], polygon)]
        if outside:
            report.warn(f"{where}: geometry point(s) {outside} lie outside room shape '{room['svg_id']}'")


def _build_scenes(scene_rows, brightness_rows, devices, report: Report) -> dict:
    scenes = []
    for i, row in enumerate(scene_rows, start=2):
        key = row.get("key")
        if not key:
            report.error(f"Scenes row {i}: key missing")
            continue
        scenes.append(
            {
                "id": row.get("scene_id"),
                "key": key,
                "display_name": row.get("display_name") or key,
                "show_in_app": _yes(row.get("show_in_app"), f"Scenes row {i} show_in_app", report),
                "sort_order": row.get("sort_order") or 0,
            }
        )
    keys = {s["key"] for s in scenes}
    for phase in PHASE_SCENES:
        if phase not in keys:
            report.error(f"Scenes: phase scene {phase!r} missing")

    by_name = {d["friendly_name"]: d for d in devices}
    brightness: dict[str, dict[str, dict]] = {}
    for i, row in enumerate(brightness_rows, start=2):
        name = row.get("friendly_name")
        where = f"SceneBrightness row {i} ({name})"
        device = by_name.get(name)
        if device is None:
            report.error(f"{where}: unknown device")
            continue
        if row.get("room") != device["room"]:
            report.error(f"{where}: room {row.get('room')!r} differs from Devices ({device['room']!r})")
        dimmable = str(row.get("dimmable") or "").lower().startswith("yes")
        caps = device["capabilities"] or {}
        if caps and dimmable != bool(caps.get("brightness")):
            report.error(f"{where}: dimmable={row.get('dimmable')!r} but Z2M brightness={caps.get('brightness')}")
        values = {}
        for phase in PHASE_SCENES:
            values[phase] = _scene_value(row.get(phase), dimmable, f"{where} {phase}", report)
        brightness[name] = values

    for device in devices:
        if device["category"] == "light" and device["in_app"] and device["friendly_name"] not in brightness:
            report.error(f"SceneBrightness: no row for light {device['friendly_name']}")

    return {"scenes": scenes, "brightness": brightness}


def _scene_value(value: Any, dimmable: bool, where: str, report: Report) -> dict:
    """Normalise one grid cell to {"on": bool, "pct": float|None}."""
    text = str(value).strip().lower() if value is not None else ""
    if text == "off":
        return {"on": False, "pct": None}
    if not dimmable:
        if text == "on":
            return {"on": True, "pct": None}
        report.error(f"{where}: on/off-only lamp needs 'on' or 'off', got {value!r}")
        return {"on": False, "pct": None}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 1 <= value <= 100:
            return {"on": True, "pct": round(float(value), 1)}
        report.error(f"{where}: brightness must be 1-100 or 'off', got {value!r}")
        return {"on": False, "pct": None}
    report.error(f"{where}: brightness must be 1-100 or 'off', got {value!r}")
    return {"on": False, "pct": None}


def _check_groups(devices, rooms, z2m_groups, report: Report) -> None:
    """Sheet flags vs. Z2M group membership."""
    def members(group: str) -> set[str] | None:
        g = z2m_groups.get(group)
        return {m["friendly_name"] for m in g["members"]} if g else None

    all_lamps = members(ALL_LAMPS_GROUP)
    if all_lamps is None:
        report.warn(f"Z2M group {ALL_LAMPS_GROUP} not in snapshot")
    else:
        flagged = {d["friendly_name"] for d in devices if d["in_all_off"]}
        for name in sorted(flagged - all_lamps):
            report.warn(f"{name}: in_all_off = yes but not a member of {ALL_LAMPS_GROUP}")
        for name in sorted(all_lamps - flagged):
            report.warn(f"{name}: member of {ALL_LAMPS_GROUP} but in_all_off = no")
        for name in sorted(all_lamps & PROTECTED_DEVICES):
            report.error(f"{name}: protected device is a member of {ALL_LAMPS_GROUP}")

    for room in rooms:
        group_members = members(room["z2m_group"])
        if group_members is None:
            continue
        lights = {d["friendly_name"] for d in devices if d["room"] == room["room"] and d["category"] == "light"}
        for name in sorted(lights - group_members):
            report.warn(f"{name}: light in room {room['room']} but not in Z2M group {room['z2m_group']}")
        for name in sorted(group_members - lights):
            report.warn(f"{name}: in Z2M group {room['z2m_group']} but not a light of that room in the sheet")


# --------------------------------------------------------------------------- main


def write_config(config: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, content in config.items():
        path = out_dir / filename
        header = {
            "_generated": "by tools/import_sheet.py from docs/devices.xlsx - do not edit by hand",
        }
        text = json.dumps({**header, **content}, ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--svg", type=Path, default=DEFAULT_SVG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = parser.parse_args(argv)

    sheets = read_workbook(args.xlsx)
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    svg_text = args.svg.read_text(encoding="utf-8")
    config, report = build_config(sheets, snapshot, svg_text)

    for msg in report.warnings:
        print(f"WARNING  {msg}")
    for msg in report.errors:
        print(f"ERROR    {msg}")
    if report.errors:
        print(f"\n{len(report.errors)} error(s) - nothing written. Fix the sheet and run again.")
        return 1
    if args.check:
        print(f"OK - {len(report.warnings)} warning(s), nothing written (--check).")
        return 0
    for path in write_config(config, args.out):
        print(f"wrote {path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}")
    print(f"OK - {len(report.warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
