"""Day phases and colour schedule (pure functions, explicit times)."""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.clock import LOCAL_TZ
from app.domain.colour import (
    ColourEvent,
    colour_at,
    events_from_boundaries,
    from_mired,
    to_mired,
)
from app.domain.phases import ManualAdvance, PhaseTimeline, scheduled_boundaries, sunset
from app.domain.settings import ScheduleSettings

DEFAULT = ScheduleSettings()
SUN = ScheduleSettings(sun_mode=True)


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LOCAL_TZ)


def hm(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%H:%M")


# --------------------------------------------------------------------------- fixed times


@pytest.mark.parametrize(
    "clock,phase",
    [((0, 0), "night"), ((6, 59), "night"), ((7, 0), "kalt"), ((17, 59), "kalt"), ((18, 0), "warm"),
     ((21, 0), "twilight"), ((22, 29), "twilight"), ((22, 30), "night"), ((23, 59), "night")],
)
def test_default_phases(clock, phase):
    now = at(2026, 11, 12, *clock)
    assert PhaseTimeline(DEFAULT, now).phase == phase


def test_phase_bar_segments_default_day():
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 12, 12))
    segs = [(hm(s.start), hm(s.end), s.phase) for s in tl.segments(date(2026, 11, 12))]
    assert segs == [
        ("00:00", "07:00", "night"),
        ("07:00", "18:00", "kalt"),
        ("18:00", "21:00", "warm"),
        ("21:00", "22:30", "twilight"),
        ("22:30", "00:00", "night"),
    ]


def test_settings_validation_and_roundtrip():
    bad = ScheduleSettings(warm=time(6, 0))
    assert bad.validate()
    assert DEFAULT.validate() == []
    s = ScheduleSettings.from_dict({"warm": "17:30", "sun_mode": True, "warm_sun": {"offset_min": -15}})
    assert s.warm == time(17, 30) and s.sun_mode and s.warm_sun.offset_min == -15
    assert s.warm_sun.earliest == time(18, 0)
    assert ScheduleSettings.from_dict(s.to_dict()) == s


# --------------------------------------------------------------------------- sun mode


def test_sun_mode_winter_clamps_to_earliest():
    # 12 Nov in Köln: sunset ~16:50 -> warm clamped to 18:00, twilight (sunset+1h) to 21:00
    day = date(2026, 11, 12)
    assert sunset(SUN, day).hour == 16
    b = {x.phase: hm(x.at) for x in scheduled_boundaries(SUN, day)}
    assert b == {"kalt": "07:00", "warm": "18:00", "twilight": "21:00", "night": "22:30"}


def test_sun_mode_summer_clamps_to_latest():
    # 21 Jun: sunset ~21:52 -> warm clamped to 21:00, twilight to 22:00
    b = {x.phase: hm(x.at) for x in scheduled_boundaries(SUN, date(2026, 6, 21))}
    assert (b["warm"], b["twilight"]) == ("21:00", "22:00")


def test_sun_mode_spring_follows_sunset():
    day = date(2026, 4, 15)
    ss = sunset(SUN, day)
    b = {x.phase: x.at for x in scheduled_boundaries(SUN, day)}
    assert 20 <= ss.hour <= 21
    assert b["warm"] == ss
    assert b["twilight"] == ss + timedelta(hours=1)


def test_sun_mode_off_ignores_sun():
    b = {x.phase: hm(x.at) for x in scheduled_boundaries(DEFAULT, date(2026, 6, 21))}
    assert (b["warm"], b["twilight"]) == ("18:00", "21:00")


# --------------------------------------------------------------------------- manual advance / never backwards


def test_spec_example_advance_to_night_at_20():
    """Advanced to night at 20:00 -> twilight at 21:00 skipped, day restarts with kalt 07:00."""
    manual = [ManualAdvance(at(2026, 11, 12, 18, 30), "twilight"), ManualAdvance(at(2026, 11, 12, 20, 0), "night")]
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 12, 20, 0), manual)
    assert tl.phase == "night"
    assert not tl.can_advance()
    assert tl.phase_at(at(2026, 11, 12, 21, 30)) == "night"
    nxt = tl.next_boundary(tl.now)
    assert (nxt.phase, nxt.at) == ("kalt", at(2026, 11, 13, 7, 0))
    segs = [(hm(s.start), s.phase) for s in tl.segments(date(2026, 11, 12))]
    assert segs == [("00:00", "night"), ("07:00", "kalt"), ("18:00", "warm"), ("18:30", "twilight"), ("20:00", "night")]


def test_advance_kalt_to_warm_skips_scheduled_warm():
    manual = [ManualAdvance(at(2026, 11, 12, 16, 0), "warm")]
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 12, 16, 0), manual)
    assert tl.phase == "warm"
    nxt = tl.next_boundary(tl.now)
    assert (nxt.phase, hm(nxt.at)) == ("twilight", "21:00")  # the 18:00 warm is skipped
    assert tl.phase_at(at(2026, 11, 12, 19)) == "warm"


def test_next_phase_order():
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 12, 12))
    assert tl.phase == "kalt" and tl.next_phase() == "warm"
    assert PhaseTimeline(DEFAULT, at(2026, 11, 12, 23)).next_phase() is None


def test_old_manual_advance_has_no_effect_next_day():
    manual = [ManualAdvance(at(2026, 11, 11, 20, 0), "night")]
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 12, 19), manual)
    assert tl.phase == "warm"


def test_edited_time_applies_immediately():
    # it is 17:30; Kolja moves warm from 18:00 to 17:00 -> warm already started
    s = ScheduleSettings(warm=time(17, 0))
    assert PhaseTimeline(s, at(2026, 11, 12, 17, 30)).phase == "warm"


# --------------------------------------------------------------------------- early day start (sunrise)


def test_wake_before_kalt_starts_day_early():
    wakes = {date(2026, 11, 13): at(2026, 11, 13, 6, 15)}
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 13, 6, 20), wakes=wakes)
    assert tl.phase == "kalt"
    first = tl.last_boundary(tl.now)
    assert (first.source, hm(first.at)) == ("wake", "06:15")
    # no second kalt boundary at 07:00
    assert [b for b in tl.boundaries if b.at.date() == date(2026, 11, 13) and b.phase == "kalt"] == [first]


def test_wake_after_kalt_changes_nothing():
    wakes = {date(2026, 11, 13): at(2026, 11, 13, 8, 0)}
    tl = PhaseTimeline(DEFAULT, at(2026, 11, 13, 7, 30), wakes=wakes)
    b = tl.last_boundary(tl.now)
    assert (b.phase, b.source, hm(b.at)) == ("kalt", "schedule", "07:00")


# --------------------------------------------------------------------------- DST


def test_dst_spring_forward_day():
    # 29 Mar 2026: clocks jump 02:00 -> 03:00. Phases still at local wall-clock times.
    tl = PhaseTimeline(DEFAULT, at(2026, 3, 29, 12))
    segs = [(hm(s.start), s.phase) for s in tl.segments(date(2026, 3, 29))]
    assert segs[1] == ("07:00", "kalt")
    total = sum((s.end.astimezone(timezone.utc) - s.start.astimezone(timezone.utc)).total_seconds()
                for s in tl.segments(date(2026, 3, 29)))
    assert total == 23 * 3600


def test_dst_fall_back_day():
    tl = PhaseTimeline(DEFAULT, at(2026, 10, 25, 12))
    total = sum((s.end.astimezone(timezone.utc) - s.start.astimezone(timezone.utc)).total_seconds()
                for s in tl.segments(date(2026, 10, 25)))
    assert total == 25 * 3600
    assert hm(tl.last_boundary(tl.now).at) == "07:00"


def test_time_in_dst_gap_is_handled():
    s = ScheduleSettings(kalt=time(2, 30))
    b = scheduled_boundaries(s, date(2026, 3, 29))[0]
    assert hm(b.at) == "03:30"  # 02:30 does not exist that night


# --------------------------------------------------------------------------- colour


def test_mired_mapping():
    assert to_mired(0) == 153 and to_mired(1) == 370 and to_mired(0.5) == 262
    assert to_mired(-1) == 153 and to_mired(7) == 370
    assert from_mired(370) == 1.0 and from_mired(500) == 1.0


def colour(settings, now, manual=(), wakes=None, overrides=()):
    tl = PhaseTimeline(settings, now, manual, wakes)
    events = events_from_boundaries(tl.boundaries, settings) + list(overrides)
    return colour_at(events, now)


@pytest.mark.parametrize(
    "clock,expected",
    [((6, 0), 1.0), ((7, 0), 1.0), ((7, 15), 0.5), ((7, 30), 0.0), ((12, 0), 0.0),
     ((18, 0), 0.0), ((18, 30), 0.5), ((19, 0), 1.0), ((23, 0), 1.0)],
)
def test_colour_ramps(clock, expected):
    assert colour(DEFAULT, at(2026, 11, 12, *clock)).master_ct == pytest.approx(expected)


def test_ramp_durations_editable():
    s = ScheduleSettings(warm_ramp_min=120, kalt_ramp_min=0)
    assert colour(s, at(2026, 11, 12, 19)).master_ct == pytest.approx(0.5)
    assert colour(s, at(2026, 11, 12, 7, 0)).master_ct == 0.0  # instant


def test_colour_ramping_flag():
    assert colour(DEFAULT, at(2026, 11, 12, 18, 30)).ramping
    assert not colour(DEFAULT, at(2026, 11, 12, 20)).ramping


def test_override_holds_until_next_ramp():
    ov = [ColourEvent(at(2026, 11, 12, 10), "override", 0.8)]
    assert colour(DEFAULT, at(2026, 11, 12, 15), overrides=ov).master_ct == 0.8
    assert colour(DEFAULT, at(2026, 11, 12, 15), overrides=ov).override
    # warm ramp at 18:00 starts *from* 0.8 (no jump back to 0) and reaches 1 at 19:00
    c = colour(DEFAULT, at(2026, 11, 12, 18, 30), overrides=ov)
    assert c.master_ct == pytest.approx(0.9) and not c.override
    assert colour(DEFAULT, at(2026, 11, 12, 19), overrides=ov).master_ct == pytest.approx(1.0)


def test_override_during_ramp_stops_it():
    ov = [ColourEvent(at(2026, 11, 12, 18, 15), "override", 0.1)]
    assert colour(DEFAULT, at(2026, 11, 12, 18, 45), overrides=ov).master_ct == pytest.approx(0.1)


def test_manual_advance_jumps_colour_and_ends_override():
    ov = [ColourEvent(at(2026, 11, 12, 10), "override", 0.3)]
    manual = [ManualAdvance(at(2026, 11, 12, 16), "warm")]
    c = colour(DEFAULT, at(2026, 11, 12, 16, 0, ), manual, overrides=ov)
    assert c.master_ct == 1.0 and not c.override
    # no ramp at the skipped 18:00 warm: stays 1
    assert colour(DEFAULT, at(2026, 11, 12, 18, 30), manual).master_ct == 1.0


def test_early_day_start_jumps_to_cold():
    wakes = {date(2026, 11, 13): at(2026, 11, 13, 6, 0)}
    assert colour(DEFAULT, at(2026, 11, 13, 6, 0), wakes=wakes).master_ct == 0.0
    assert colour(DEFAULT, at(2026, 11, 13, 5, 59), wakes=wakes).master_ct == 1.0


def test_restart_mid_ramp_lands_on_same_value():
    """Value is a pure function of time and persisted events."""
    a = colour(DEFAULT, at(2026, 11, 12, 18, 40)).master_ct
    b = colour(DEFAULT, at(2026, 11, 12, 18, 40)).master_ct
    assert a == b == pytest.approx(40 / 60)


def test_ramp_across_dst_uses_real_minutes():
    # kalt ramp on the fall-back day: 07:00 + 15 real minutes -> half way
    assert colour(DEFAULT, at(2026, 10, 25, 7, 15)).master_ct == pytest.approx(0.5)
