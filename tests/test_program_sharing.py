"""Programs are private until made public, and a coach can send one to many athletes."""

from src.config import RATE_LIMIT_MAX

from .conftest import add_program, befriend
from .test_coaching import link


def names(user):
    return [p["name"] for p in user.get("/programs")[1]]


def test_programs_are_private_by_default(make_user):
    me, friend, coach = make_user("me"), make_user("friend"), make_user("coach")
    befriend(me, friend)
    link(coach, me)
    pid = add_program(me, public=False)
    assert me.get("/programs")[1][0]["is_public"] is False
    assert friend.get(f"/athletes/{me.id}/programs")[1] == []
    assert friend.get(f"/athletes/{me.id}/programs/{pid}")[0] == 404
    assert friend.post(f"/athletes/{me.id}/programs/{pid}/copy")[0] == 404
    assert coach.get(f"/coached/{me.id}/programs")[1] == []
    assert coach.get(f"/coached/{me.id}/programs/{pid}")[0] == 404


def test_making_public_and_private_again(make_user):
    me, friend, coach = make_user("me"), make_user("friend"), make_user("coach")
    befriend(me, friend)
    link(coach, me)
    pid = add_program(me, public=False)
    assert me.put(f"/programs/{pid}", {"is_public": True})[1]["is_public"]
    assert [p["id"] for p in friend.get(f"/athletes/{me.id}/programs")[1]] == [pid]
    assert friend.post(f"/athletes/{me.id}/programs/{pid}/copy")[0] == 201
    assert coach.get(f"/coached/{me.id}/programs/{pid}")[0] == 200
    me.put(f"/programs/{pid}", {"is_public": False})
    assert friend.get(f"/athletes/{me.id}/programs/{pid}")[0] == 404
    assert coach.get(f"/coached/{me.id}/programs")[1] == []


def test_is_public_must_be_true_or_false(make_user):
    me = make_user("me")
    pid = add_program(me, public=False)
    assert me.put(f"/programs/{pid}", {"is_public": "yes"})[0] == 400


def test_a_stranger_never_sees_a_public_program(make_user):
    me, stranger = make_user("me"), make_user("stranger")
    pid = add_program(me, public=True)
    assert stranger.get(f"/athletes/{me.id}/programs/{pid}")[0] == 404


def test_coach_sends_a_private_program_to_many_athletes(make_user):
    coach, a1, a2, other = make_user("coach"), make_user("one"), make_user("two"), make_user("other")
    link(coach, a1)
    link(coach, a2)
    pid = add_program(coach, sets=2, public=False)
    code, body = coach.post(f"/programs/{pid}/send", {"athlete_ids": [a1.id, a2.id, a1.id, other.id]})
    assert code == 200
    assert [(r["athlete_id"], r["ok"]) for r in body["results"]] == [(a1.id, True), (a2.id, True), (other.id, False)]
    assert body["results"][2]["error"] == "Not one of your athletes"
    for a in (a1, a2):
        [received] = a.get("/programs")[1]
        assert received["name"] == "Push" and received["is_public"] is False
        assert received["assigned_by_name"] == "coach"
        assert [len(d["sets"]) for d in a.get(f"/programs/{received['id']}")[1]["days"]] == [2]
    assert names(other) == []
    assert len(names(coach)) == 1  # the original is untouched and still private


def test_send_rules(make_user):
    coach, a, stranger = make_user("coach"), make_user("athlete"), make_user("stranger")
    pid = add_program(coach)
    coach.post("/coaching-requests", {"username": "athlete"})  # not accepted yet
    assert coach.post(f"/programs/{pid}/send", {"athlete_ids": [a.id]})[1]["results"][0]["ok"] is False
    assert coach.post(f"/programs/{pid}/send", {"athlete_ids": []})[0] == 400
    assert coach.post(f"/programs/{pid}/send", {"athlete_ids": ["x"]})[0] == 400
    assert coach.post(f"/programs/{pid}/send", {"athlete_ids": list(range(1, 60))})[0] == 400
    # Only the owner can send a program, and an athlete cannot "send" to their coach.
    assert stranger.post(f"/programs/{pid}/send", {"athlete_ids": [a.id]})[0] == 404
    assert a.put(f"/coaching-requests/{coach.id}", {"status": "accepted"})[0] == 200
    assert a.post(f"/programs/{add_program(a)}/send", {"athlete_ids": [coach.id]})[1]["results"][0]["ok"] is False


def test_sending_is_rate_limited(make_user):
    coach, a = make_user("coach"), make_user("athlete")
    link(coach, a)
    pid = add_program(coach)
    for _ in range(RATE_LIMIT_MAX):
        assert coach.post(f"/programs/{pid}/send", {"athlete_ids": [a.id]})[0] == 200
    assert coach.post(f"/programs/{pid}/send", {"athlete_ids": [a.id]})[0] == 429
