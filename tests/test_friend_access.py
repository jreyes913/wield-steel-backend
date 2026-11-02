"""A friend's data is visible to accepted friends only, and never includes private fields."""

import pytest

from .conftest import add_lift, add_program, befriend

PRIVATE = ("body_weight", "dob", "gender", "height", "cognito_sub")


def routes(owner_id, lift_id, program_id):
    return [
        ("GET", f"/athletes/{owner_id}"),
        ("GET", f"/athletes/{owner_id}/lifts"),
        ("GET", f"/athletes/{owner_id}/lifts/{lift_id}"),
        ("GET", f"/athletes/{owner_id}/programs"),
        ("GET", f"/athletes/{owner_id}/programs/{program_id}"),
        ("POST", f"/athletes/{owner_id}/programs/{program_id}/copy"),
    ]


@pytest.fixture
def owner(make_user):
    o = make_user("owner")
    o.put("/me", {"first_name": "Olive", "dob": "1990-01-01", "gender": "Female", "height": 66})
    return o, add_lift(o), add_program(o)


def make_state(state, owner_user, other):
    if state == "pending":
        other.post("/friend-requests", {"username": owner_user.username})
    elif state == "declined":
        other.post("/friend-requests", {"username": owner_user.username})
        owner_user.put(f"/friend-requests/{other.id}", {"status": "declined"})
    elif state == "blocked_by_owner":
        owner_user.put(f"/blocks/{other.id}")
    elif state == "blocked_by_other":
        other.put(f"/blocks/{owner_user.id}")


@pytest.mark.parametrize("state", ["stranger", "pending", "declined", "blocked_by_owner", "blocked_by_other"])
def test_non_friends_get_404_everywhere(make_user, owner, state):
    o, lift_id, program_id = owner
    other = make_user("other")
    make_state(state, o, other)
    for method, path in routes(o.id, lift_id, program_id):
        assert other.call(method, path)[0] == 404, (state, method, path)


def test_accepted_friend_gets_data_without_private_fields(make_user, owner):
    o, lift_id, program_id = owner
    friend = make_user("friend")
    befriend(friend, o)
    for method, path in routes(o.id, lift_id, program_id)[:-1]:
        code, body = friend.call(method, path)
        assert code == 200, path
        text = str(body)
        for field in PRIVATE:
            assert field not in text, (path, field)
    assert friend.get(f"/athletes/{o.id}/lifts")[1] == [{"id": lift_id, "date": "2026-01-05"}]
    assert friend.get(f"/athletes/{o.id}/lifts/{lift_id}")[1]["sets"][0]["subsets"][0]["weight"] == 225
    assert friend.get(f"/athletes/{o.id}")[1] == {
        "id": o.id, "username": "owner", "first_name": "Olive", "last_name": None,
    }


def test_friend_lists_never_expose_private_fields(make_user, owner):
    o, *_ = owner
    friend = make_user("friend")
    befriend(friend, o)
    friend.put("/me", {"dob": "1991-02-02", "height": 70})
    for body in (o.get("/friends")[1], o.get("/friend-requests")[1], o.get("/blocks")[1]):
        for field in PRIVATE:
            assert field not in str(body)


def test_friends_cannot_write_to_each_others_data(make_user, owner):
    o, lift_id, program_id = owner
    friend = make_user("friend")
    befriend(friend, o)
    assert friend.put(f"/lifts/{lift_id}", {"date": "2026-02-02"})[0] == 404
    assert friend.delete(f"/lifts/{lift_id}")[0] == 404
    assert friend.delete(f"/programs/{program_id}")[0] == 404
    assert friend.get(f"/lifts/{lift_id}")[0] == 404


def test_hiding_lifts_is_one_way_and_invisible(make_user, owner):
    o, lift_id, _ = owner
    friend = make_user("friend")
    add_lift(friend)
    befriend(friend, o)
    assert o.put(f"/friends/{friend.id}", {"hide_my_lifts": True})[0] == 200
    # The friend sees an empty list / a missing lift, exactly as for a friend with none.
    assert friend.get(f"/athletes/{o.id}/lifts") == (200, [])
    assert friend.get(f"/athletes/{o.id}/lifts/{lift_id}")[0] == 404
    assert friend.get(f"/athletes/{o.id}/lifts/999999")[0] == 404
    # One-way: the owner still sees the friend's lifts.
    assert len(o.get(f"/athletes/{friend.id}/lifts")[1]) == 1
    # The friend's own view of the friendship shows no sign of it, and programs stay visible.
    assert friend.get("/friends")[1][0]["hide_my_lifts"] is False
    assert friend.get(f"/athletes/{o.id}/programs")[0] == 200
    # Unhiding restores access.
    o.put(f"/friends/{friend.id}", {"hide_my_lifts": False})
    assert len(friend.get(f"/athletes/{o.id}/lifts")[1]) == 1
