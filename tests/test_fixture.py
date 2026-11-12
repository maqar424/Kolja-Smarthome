"""Sanity check that the test setup and the snapshot fixture work."""


def test_snapshot_has_rooms_and_all_lamps_group(z2m_snapshot):
    groups = {g["friendly_name"] for g in z2m_snapshot["groups"]}
    assert {"Wohnzimmer", "Küche", "Bad", "Flur", "Alle_Lampen"} <= groups


def test_pveswitch_is_not_in_any_group(z2m_snapshot):
    for group in z2m_snapshot["groups"]:
        assert all(m["friendly_name"] != "pveSwitch" for m in group["members"])
