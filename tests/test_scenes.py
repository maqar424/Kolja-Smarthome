import pytest

from app.config import AppConfig
from app.domain.scenes import (
    LampTarget,
    RoomLight,
    level,
    next_factor,
    pct_to_level,
    scene_bases,
    scene_targets,
)


@pytest.fixture(scope="module")
def config():
    return AppConfig.load()


def test_scene_targets_full_apply_incl_off_and_plugs(config):
    t = scene_targets(config, "Wohnzimmer", "twilight")
    assert t["wzBettlampe"] == LampTarget(True, round(0.37 * 254))
    assert t["wzDeckenlampe"] == LampTarget(False)
    assert t["wzTowerlampe"] == LampTarget(False)
    warm = scene_targets(config, "Wohnzimmer", "warm")
    assert warm["wzTowerlampe"] == LampTarget(True, None)  # plug: on, no brightness
    assert warm["wzDeckenlampe"] == LampTarget(True, 254)


def test_night_values_never_round_to_zero(config):
    t = scene_targets(config, "Küche", "night")
    assert t["kuecheTwintowers"].brightness == 3  # 1.2 % of 254 = 3.05
    assert t["kuecheTischlampe"] == LampTarget(True, 254)  # intended 100 % at night


def test_scene_bases_only_dimmable_lamps_that_are_on(config):
    bases = scene_bases(config, "Wohnzimmer", "twilight")
    assert set(bases) == {"wzBettlampe", "wzBodenstrips", "wzSchreibtischlampe"}
    assert bases["wzBettlampe"] == pytest.approx(pct_to_level(37))


def test_level_bounds():
    assert level(0.2, 1) == 1
    assert level(300, 1) == 254
    assert level(100, 1.15) == 115


# --------------------------------------------------------------------------- dimming


def test_dim_up_stops_exactly_at_100_percent():
    bases = [pct_to_level(50), pct_to_level(100) / 2]  # both 127
    f = 1.0
    steps = 0
    while (new := next_factor(bases, f, "up")) is not None:
        f, steps = new, steps + 1
    assert level(max(bases), f) == 254
    assert steps == 5  # 1.15^4 = 1.75 < 2, 5th step capped at 2.0
    assert f == pytest.approx(2.0)


def test_dim_up_at_full_does_nothing():
    assert next_factor([254.0], 1.0, "up") is None


def test_dim_preserves_ratios():
    bases = [254.0, 127.0]
    f = next_factor(bases, 1.0, "down")
    a, b = level(bases[0], f), level(bases[1], f)
    assert a / b == pytest.approx(2.0, rel=0.02)
    # a lamp at 100 % changes twice as much as one at 50 %
    assert (254 - a) == pytest.approx(2 * (127 - b), abs=1)


def test_dim_down_stops_before_any_lamp_rounds_to_zero():
    bases = [pct_to_level(1.2), pct_to_level(100)]  # night-like: 3.05 and 254
    f = 1.0
    while (new := next_factor(bases, f, "down")) is not None:
        f = new
    assert round(bases[0] * f) >= 1
    assert round(bases[0] * f * 0.85) < 1  # one more step would turn it off
    assert all(level(b, f) >= 1 for b in bases)


def test_dim_without_dimmable_lamps_on():
    assert next_factor([], 1.0, "up") is None
    assert next_factor([], 1.0, "down") is None


def test_dim_bad_direction():
    with pytest.raises(ValueError):
        next_factor([100.0], 1.0, "sideways")


def test_room_light_roundtrip():
    r = RoomLight("warm", 0.85, {"wzBettlampe": 254.0})
    assert RoomLight.from_dict(r.to_dict()) == r
    assert RoomLight.from_dict(None) == RoomLight()
