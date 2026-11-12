from datetime import date, datetime, time

import pytest

from app.clock import LOCAL_TZ
from app.domain.sunrise import (
    SunriseSettings,
    frame,
    lamp_brightness,
    mired_at,
    occurrence_on,
    perceptual,
    relevant_occurrence,
)


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LOCAL_TZ)


def test_perceptual_curve_endpoints_and_monotonic():
    assert perceptual(0) == 0 and perceptual(1) == pytest.approx(1)
    values = [perceptual(i / 100) for i in range(101)]
    assert values == sorted(values)
    assert perceptual(0.5) == pytest.approx(0.184, abs=0.002)  # half lightness = ~18 % luminance


def test_dark_start_has_no_jump():
    """First minutes stay very low: the steps between frames are tiny at the start."""
    levels = [lamp_brightness(i / 360, 0.0) for i in range(0, 37)]  # first 10 % of a 30-min run, every 5 s
    assert levels[0] == 1
    assert max(b - a for a, b in zip(levels, levels[1:])) <= 1
    assert levels[-1] <= 4


def test_lamps_switch_on_in_turn():
    assert frame(0.0) == {"wzBodenstrips": 1, "wzSchreibtischlampe": None, "wzDeckenlampe": None}
    f = frame(0.5)
    assert f["wzSchreibtischlampe"] >= 1 and f["wzDeckenlampe"] is None
    assert frame(2 / 3)["wzDeckenlampe"] == 1
    assert frame(1.0) == {"wzBodenstrips": 254, "wzSchreibtischlampe": 254, "wzDeckenlampe": 254}


def test_never_off_once_on():
    for name_start in (0.0, 1 / 3, 2 / 3):
        for i in range(101):
            t = i / 100
            b = lamp_brightness(t, name_start)
            assert b is None or 1 <= b <= 254


def test_colour_ramp_stays_warm_while_dim():
    assert mired_at(0) == 370 and mired_at(1) == 153 and mired_at(2) == 153
    assert mired_at(0.5) >= 325  # ~3000 K at half time (linear would be 262)
    assert mired_at(2 / 3) >= 280  # still warm when the ceiling lamp comes on
    values = [mired_at(i / 100) for i in range(101)]
    assert values == sorted(values, reverse=True)  # only ever colder


def test_occurrence_and_t():
    s = SunriseSettings(enabled=True, wake=time(6, 30), duration_min=30)
    occ = occurrence_on(s, date(2026, 11, 13))
    assert occ.start == at(2026, 11, 13, 6, 0) and occ.wake == at(2026, 11, 13, 6, 30)
    assert occ.t(at(2026, 11, 13, 6, 15)) == pytest.approx(0.5)
    assert occ.t(at(2026, 11, 13, 5, 0)) == 0 and occ.t(at(2026, 11, 13, 7, 0)) == 1


def test_occurrence_starts_previous_evening_for_early_wake():
    s = SunriseSettings(wake=time(0, 20), duration_min=30)
    occ = occurrence_on(s, date(2026, 11, 13))
    assert occ.start == at(2026, 11, 12, 23, 50)


def test_occurrence_across_dst_uses_real_minutes():
    s = SunriseSettings(wake=time(3, 15), duration_min=30)  # 29 Mar: 02:00-03:00 doesn't exist
    occ = occurrence_on(s, date(2026, 3, 29))
    assert (occ.wake.timestamp() - occ.start.timestamp()) == 30 * 60
    assert occ.start.strftime("%H:%M") == "01:45"


@pytest.mark.parametrize(
    "now,expected_day",
    [
        (at(2026, 11, 12, 20, 0), date(2026, 11, 13)),  # evening -> tomorrow
        (at(2026, 11, 13, 6, 10), date(2026, 11, 13)),  # running
        (at(2026, 11, 13, 6, 45), date(2026, 11, 13)),  # after wake, inside finishing window
        (at(2026, 11, 13, 7, 1), date(2026, 11, 14)),  # window over -> next day
    ],
)
def test_relevant_occurrence(now, expected_day):
    s = SunriseSettings(enabled=True, wake=time(6, 30), duration_min=30)
    assert relevant_occurrence(s, now).day == expected_day


def test_settings_roundtrip_and_validation():
    s = SunriseSettings(enabled=True, wake=time(5, 45), duration_min=40)
    assert SunriseSettings.from_dict(s.to_dict()) == s
    assert SunriseSettings(duration_min=2).validate()
    assert SunriseSettings.from_dict(None) == SunriseSettings()
