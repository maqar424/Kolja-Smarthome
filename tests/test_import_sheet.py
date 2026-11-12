"""Tests for tools/import_sheet.py (Excel master sheet -> config/*.json)."""

import copy
import importlib.util
import json

import pytest

from tests.conftest import REPO_ROOT

_spec = importlib.util.spec_from_file_location("import_sheet", REPO_ROOT / "tools" / "import_sheet.py")
imp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(imp)


@pytest.fixture(scope="module")
def sheets():
    return imp.read_workbook(REPO_ROOT / "docs" / "devices.xlsx")


@pytest.fixture(scope="module")
def svg_text():
    return (REPO_ROOT / "docs" / "apartment-floorplan.svg").read_text(encoding="utf-8")


def build(sheets, snapshot, svg_text):
    return imp.build_config(sheets, snapshot, svg_text)


def row(sheets, sheet, name, key="friendly_name"):
    return next(r for r in sheets[sheet] if r.get(key) == name)


# --------------------------------------------------------------------------- real sheet


def test_real_sheet_is_clean(sheets, z2m_snapshot, svg_text):
    _, report = build(sheets, z2m_snapshot, svg_text)
    assert report.errors == []
    assert report.warnings == []


def test_committed_config_is_up_to_date(sheets, z2m_snapshot, svg_text, tmp_path):
    """Fails if someone edited the sheet but forgot to run the import (or edited JSON by hand)."""
    config, _ = build(sheets, z2m_snapshot, svg_text)
    imp.write_config(config, tmp_path)
    for filename in config:
        fresh = (tmp_path / filename).read_text(encoding="utf-8")
        committed = (REPO_ROOT / "config" / filename).read_text(encoding="utf-8")
        assert fresh == committed, f"config/{filename} is stale: run python tools/import_sheet.py"


def test_notes_below_scene_grid_are_ignored(sheets):
    names = [r["friendly_name"] for r in sheets["SceneBrightness"]]
    assert names[-1] == "flurDeckenstrip"
    assert len(names) == 14


def test_capabilities_and_protection(sheets, z2m_snapshot, svg_text):
    config, _ = build(sheets, z2m_snapshot, svg_text)
    devices = {d["friendly_name"]: d for d in config["devices.json"]["devices"]}
    assert devices["pveSwitch"]["protected"] is True
    assert devices["pveSwitch"]["capabilities"]["power"] is True
    assert devices["wzTowerlampe"]["capabilities"]["brightness"] is False
    assert devices["wzTowerlampe"]["capabilities"]["color_temp"] is None
    assert devices["wzDeckenlampe"]["capabilities"]["color_temp"] == [153, 454]
    assert "on_hold" in devices["flurLichtschalter"]["capabilities"]["actions"]


def test_scene_values_normalised(sheets, z2m_snapshot, svg_text):
    config, _ = build(sheets, z2m_snapshot, svg_text)
    grid = config["scenes.json"]["brightness"]
    assert grid["wzBettlampe"]["night"] == {"on": True, "pct": 5.1}
    assert grid["wzDeckenlampe"]["twilight"] == {"on": False, "pct": None}
    assert grid["wzTowerlampe"]["warm"] == {"on": True, "pct": None}
    assert grid["kuecheTischlampe"]["night"] == {"on": True, "pct": 100.0}


def test_room_display_names(sheets, z2m_snapshot, svg_text):
    config, _ = build(sheets, z2m_snapshot, svg_text)
    names = {r["room"]: r["display_name"] for r in config["rooms.json"]["rooms"]}
    assert names["Wohnzimmer"] == "Schlafzimmer"
    assert names["Bad"] == "Badezimmer"


# --------------------------------------------------------------------------- validation catches mistakes


def test_bad_scene_value_is_error(sheets, z2m_snapshot, svg_text):
    s = copy.deepcopy(sheets)
    row(s, "SceneBrightness", "wzBettlampe")["night"] = 0
    row(s, "SceneBrightness", "wzTowerlampe")["warm"] = 50
    _, report = build(s, z2m_snapshot, svg_text)
    assert any("wzBettlampe" in e and "night" in e for e in report.errors)
    assert any("wzTowerlampe" in e and "warm" in e for e in report.errors)


def test_protected_device_flags_are_error(sheets, z2m_snapshot, svg_text):
    s = copy.deepcopy(sheets)
    row(s, "Devices", "pveSwitch")["in_all_off"] = "yes"
    _, report = build(s, z2m_snapshot, svg_text)
    assert any("protected" in e for e in report.errors)


def test_pveswitch_in_all_lamps_group_is_error(sheets, z2m_snapshot, svg_text):
    snap = copy.deepcopy(z2m_snapshot)
    group = next(g for g in snap["groups"] if g["friendly_name"] == "Alle_Lampen")
    group["members"].append({"friendly_name": "pveSwitch", "endpoint": 1})
    _, report = build(sheets, snap, svg_text)
    assert any("pveSwitch" in e and "Alle_Lampen" in e for e in report.errors)


def test_in_all_off_mismatch_is_warning(sheets, z2m_snapshot, svg_text):
    s = copy.deepcopy(sheets)
    row(s, "Devices", "wzLavalampe")["in_all_off"] = "no"
    _, report = build(s, z2m_snapshot, svg_text)
    assert report.errors == []
    assert any("wzLavalampe" in w and "Alle_Lampen" in w for w in report.warnings)


def test_lamp_outside_its_room_is_warning(sheets, z2m_snapshot, svg_text):
    s = copy.deepcopy(sheets)
    row(s, "Devices", "badDeckenlampe")["geometry_cm"] = "500,300,10,10"  # in the kitchen
    _, report = build(s, z2m_snapshot, svg_text)
    assert any("badDeckenlampe" in w and "outside" in w for w in report.warnings)


def test_malformed_geometry_is_error(sheets, z2m_snapshot, svg_text):
    s = copy.deepcopy(sheets)
    row(s, "Devices", "wzBettlampe")["geometry_cm"] = "411,1213"  # line with one point
    _, report = build(s, z2m_snapshot, svg_text)
    assert any("wzBettlampe" in e and "geometry" in e for e in report.errors)


def test_device_missing_in_z2m_is_warning(sheets, z2m_snapshot, svg_text):
    snap = copy.deepcopy(z2m_snapshot)
    snap["devices"] = [d for d in snap["devices"] if d["friendly_name"] != "badSpiegellampe"]
    _, report = build(sheets, snap, svg_text)
    assert any("badSpiegellampe" in w and "snapshot" in w for w in report.warnings)


def test_narrow_colour_range_is_error(sheets, z2m_snapshot, svg_text):
    snap = copy.deepcopy(z2m_snapshot)
    dev = next(d for d in snap["devices"] if d["friendly_name"] == "flurDeckenstrip")
    for item in dev["exposes"]:
        for feat in item.get("features", []):
            if feat.get("property") == "color_temp":
                feat["value_max"] = 300
    _, report = build(sheets, snap, svg_text)
    assert any("flurDeckenstrip" in e and "common range" in e for e in report.errors)


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "shape,text,expected",
    [
        ("point", "607,961", [[607, 961]]),
        ("point", "430,74;609,76", [[430, 74], [609, 76]]),
        ("line", "1,2 3,4 5,6", [[[1, 2], [3, 4], [5, 6]]]),
        ("rect", "452,912,84,84", [[452, 912, 84, 84]]),
        ("point", "1.5, 2", [[1.5, 2]]),
    ],
)
def test_parse_geometry(shape, text, expected):
    assert imp.parse_geometry(shape, text) == expected


@pytest.mark.parametrize("shape,text", [("rect", "1,2,0,5"), ("point", "1"), ("line", "1,2"), ("blob", "1,2")])
def test_parse_geometry_rejects(shape, text):
    with pytest.raises(ValueError):
        imp.parse_geometry(shape, text)


def test_point_in_polygon_boundary_counts_as_inside():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert imp.point_in_polygon(5, 5, square)
    assert imp.point_in_polygon(10, 5, square)
    assert not imp.point_in_polygon(11, 5, square)


def test_main_check_mode_writes_nothing(tmp_path, capsys):
    assert imp.main(["--check", "--out", str(tmp_path)]) == 0
    assert list(tmp_path.iterdir()) == []
    assert "OK" in capsys.readouterr().out
