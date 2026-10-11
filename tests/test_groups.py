import pytest

from whisper_im.groups import Groups

ALICE, BOB, CAROL, MALLORY = (("10.0.0.%d" % n, 48765) for n in (1, 2, 3, 66))


def test_created_group_has_a_name_members_and_an_id_of_its_own():
    groups = Groups()
    lunch = groups.create("  Lunch crew ", [BOB, CAROL])
    assert lunch.name == "Lunch crew" and lunch.members == [BOB, CAROL]
    assert groups.create("Lunch crew", [BOB]).gid != lunch.gid
    assert groups.get(lunch.gid) is lunch


@pytest.mark.parametrize("name, members", [
    ("", [BOB]),
    ("   ", [BOB]),
    ("x" * 41, [BOB]),
    ("Lunch crew", []),
    ("Lunch crew", [("10.0.%d.1" % n, 48765) for n in range(32)]),
])
def test_group_needs_a_sensible_name_and_someone_in_it(name, members):
    with pytest.raises(ValueError):
        Groups().create(name, members)


def test_first_message_from_a_group_introduces_it():
    groups = Groups()
    lunch = groups.receive("0123456789abcdef", "Lunch crew", [ALICE, CAROL])
    assert lunch.name == "Lunch crew" and lunch.members == [ALICE, CAROL]
    assert groups.get("0123456789abcdef") is lunch


def test_name_and_members_stay_as_first_seen():
    groups = Groups()
    lunch = groups.receive("0123456789abcdef", "Lunch crew", [ALICE, CAROL])
    again = groups.receive("0123456789abcdef", "Renamed", [CAROL, ALICE, MALLORY])
    assert again is lunch
    assert lunch.name == "Lunch crew" and lunch.members == [ALICE, CAROL]


def test_outsider_cannot_speak_in_a_group():
    groups = Groups()
    groups.receive("0123456789abcdef", "Lunch crew", [ALICE, CAROL])
    assert groups.receive("0123456789abcdef", "Lunch crew", [MALLORY, ALICE]) is None


def test_member_of_a_group_i_created_can_speak_in_it():
    groups = Groups()
    lunch = groups.create("Lunch crew", [BOB, CAROL])
    assert groups.receive(lunch.gid, "Lunch crew", [BOB, CAROL]) is lunch
