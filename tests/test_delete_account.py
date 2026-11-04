"""GET /me/export and DELETE /me."""

import json

import pytest
from botocore.exceptions import ClientError

from src.routes import account

from .conftest import add_lift, add_program, befriend


def all_rows(sql):
    return {t: sql(f"SELECT COUNT(*) FROM workout.{t}")[0][0] for t in (
        "athlete", "lift", "exercise_set", "exercise_subset", "program", "program_lift",
        "program_exercise_set", "program_exercise_subset", "friendship")}


def test_export_has_my_data_and_nothing_secret(make_user):
    me, friend, blocked, stranger = (make_user(n) for n in ("me", "friend", "blocked", "stranger"))
    add_lift(me)
    add_program(me)
    befriend(friend, me)
    friend.put("/me", {"dob": "1990-01-01", "height": 70})
    me.put(f"/blocks/{blocked.id}")
    stranger.put(f"/blocks/{me.id}")  # a block against me is not mine to export
    code, data = me.get("/me/export")
    assert code == 200
    assert data["profile"]["username"] == "me"
    assert data["lifts"][0]["body_weight"] == 180
    assert data["lifts"][0]["sets"][0]["subsets"][0]["exercise"] == "Bench Press"
    assert data["programs"][0]["days"][0]["sets"][0]["subsets"][0]["reps"] == 8
    assert [f["username"] for f in data["friends"]] == ["friend"]
    assert [b["username"] for b in data["blocked"]] == ["blocked"]
    text = json.dumps(data)
    assert "cognito_sub" not in text and "sub-" not in text
    assert "stranger" not in text
    assert "dob" not in json.dumps(data["friends"]) and "height" not in json.dumps(data["friends"])


def test_delete_needs_matching_confirmation(make_user, sql, fake_db):
    me = make_user("me")
    add_lift(me)
    before = all_rows(sql)
    assert me.delete("/me", {})[0] == 400
    assert me.delete("/me", {"confirm": "someone-else"})[0] == 400
    assert all_rows(sql) == before and fake_db.cognito.deleted == []


def test_delete_removes_everything_of_mine_and_nothing_else(make_user, sql, fake_db):
    me, friend, other = make_user("me"), make_user("friend"), make_user("other")
    for _ in range(3):
        add_lift(me)
    add_program(me, sets=2)
    add_lift(other)
    add_program(other)
    befriend(friend, me)         # accepted
    other.post("/friend-requests", {"username": "me"})  # pending towards me
    friend.put(f"/blocks/{other.id}")  # unrelated to me: must survive
    blocker = make_user("blocker")
    blocker.put(f"/blocks/{me.id}")    # a block against me goes too
    me.put(f"/blocks/{make_user('victim').id}")  # and one I made
    assert me.delete("/me", {"confirm": "ME"}) == (200, {"done": True})
    assert fake_db.cognito.deleted == ["me"]
    # Their tree is gone; the others' is intact.
    assert sql("SELECT COUNT(*) FROM workout.lift WHERE athlete_id = :i", i=me.id)[0][0] == 0
    assert sql("SELECT COUNT(*) FROM workout.athlete WHERE id = :i", i=me.id)[0][0] == 0
    assert sql("SELECT COUNT(*) FROM workout.friendship WHERE requester_id = :i OR addressee_id = :i", i=me.id)[0][0] == 0
    assert sql("SELECT COUNT(*) FROM workout.friendship")[0][0] == 1  # friend -> other block
    assert all_rows(sql)["lift"] == 1 and all_rows(sql)["program"] == 1
    assert all_rows(sql)["exercise_subset"] == 1 and all_rows(sql)["program_exercise_subset"] == 1
    assert friend.get("/friends")[1] == []
    assert other.get("/friend-requests")[1]["outgoing"] == []


def test_username_can_be_taken_again_after_delete(make_user):
    me, other = make_user("mel"), make_user("other")
    me.delete("/me", {"confirm": "mel"})
    assert other.put("/me", {"username": "mel"})[1]["username"] == "mel"


def test_delete_in_rounds_when_out_of_time(make_user, sql, fake_db, monkeypatch):
    me = make_user("me")
    for _ in range(4):
        add_lift(me)
    ticks = iter(range(0, 1000))
    # A clock that jumps past the budget after a few checks.
    monkeypatch.setattr(account.time, "monotonic", lambda: next(ticks) * (account.DELETE_TIME_BUDGET_SECONDS / 3))
    code, body = me.delete("/me", {"confirm": "me"})
    assert (code, body["done"]) == (202, False)
    assert fake_db.cognito.deleted == []
    assert sql("SELECT COUNT(*) FROM workout.athlete")[0][0] == 1  # athlete row goes last
    monkeypatch.setattr(account.time, "monotonic", lambda: 0.0)
    assert me.delete("/me", {"confirm": "me"}) == (200, {"done": True})  # repeat finishes
    assert all_rows(sql)["lift"] == 0 and all_rows(sql)["athlete"] == 0


def test_cognito_failure_is_reported_after_data_is_gone(make_user, sql, fake_db):
    me = make_user("me")
    add_lift(me)
    fake_db.cognito.error = ClientError({"Error": {"Code": "InternalErrorException"}}, "AdminDeleteUser")
    code, body = me.delete("/me", {"confirm": "me"})
    assert code == 502 and "data has been deleted" in body["error"]
    assert all_rows(sql)["lift"] == 0 and all_rows(sql)["athlete"] == 0


def test_cognito_user_already_gone_counts_as_done(make_user, fake_db):
    me = make_user("me")
    fake_db.cognito.error = ClientError({"Error": {"Code": "UserNotFoundException"}}, "AdminDeleteUser")
    assert me.delete("/me", {"confirm": "me"}) == (200, {"done": True})


def test_cache_is_cleared_after_delete(make_user):
    from src import auth
    me = make_user("me")
    assert me.sub in auth._athlete_cache
    me.delete("/me", {"confirm": "me"})
    assert me.sub not in auth._athlete_cache


def test_deleted_users_token_does_not_recreate_a_profile(make_user, fake_db, sql):
    me = make_user("me")
    me.delete("/me", {"confirm": "me"})
    fake_db.cognito.gone.add("me")
    # The same (still valid) token comes back after the delete.
    assert me.call("GET", "/me")[0] == 401
    assert sql("SELECT COUNT(*) AS n FROM workout.athlete WHERE cognito_sub = :s", s=me.sub)[0][0] == 0
