from datetime import datetime, time, timedelta

from app.clock import LOCAL_TZ
from app.domain.phases import ManualAdvance
from app.domain.scenes import RoomLight
from app.domain.settings import ScheduleSettings
from app.store import Store


def test_values_survive_reopen(tmp_path):
    path = tmp_path / "data" / "app.db"
    store = Store(path)
    store.save_schedule(ScheduleSettings(warm=time(17, 45), sun_mode=True))
    store.save_room_light("Küche", RoomLight("night", 0.85, {"kuecheTwintowers": 3.05}))
    now = datetime(2026, 11, 12, 20, 0, tzinfo=LOCAL_TZ)
    store.set_colour_override(now, 0.4)
    store.add_manual_advance(ManualAdvance(now, "twilight"))
    store.set_applied_phase("twilight", now)
    store.close()

    store = Store(path)
    assert store.schedule().warm == time(17, 45) and store.schedule().sun_mode
    assert store.room_light("Küche") == RoomLight("night", 0.85, {"kuecheTwintowers": 3.05})
    assert store.colour_override().target == 0.4 and store.colour_override().at == now
    assert store.manual_advances() == [ManualAdvance(now, "twilight")]
    assert store.applied_phase() == {"phase": "twilight", "at": now.isoformat()}


def test_defaults_on_empty_store():
    store = Store(":memory:")
    assert store.schedule() == ScheduleSettings()
    assert store.room_light("Bad") == RoomLight()
    assert store.colour_override() is None
    assert store.manual_advances() == []
    assert store.applied_phase() is None


def test_old_manual_advances_are_pruned():
    store = Store(":memory:")
    now = datetime(2026, 11, 12, 20, 0, tzinfo=LOCAL_TZ)
    store.add_manual_advance(ManualAdvance(now - timedelta(days=5), "night"))
    store.add_manual_advance(ManualAdvance(now, "warm"))
    assert [m.to for m in store.manual_advances()] == ["warm"]


def test_clear_override():
    store = Store(":memory:")
    store.set_colour_override(datetime(2026, 11, 12, tzinfo=LOCAL_TZ), 0.5)
    store.clear_colour_override()
    assert store.colour_override() is None
