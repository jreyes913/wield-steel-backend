"""Coaching links: consent, read-only access, what a coach may see, and clean-up."""

from .conftest import add_lift, add_program, befriend


def link(coach, athlete, weight=None):
    """The coach asks, the athlete accepts."""
    assert coach.post("/coaching-requests", {"username": athlete.username})[0] == 201
    body = {"status": "accepted"} | ({} if weight is None else {"can_see_body_weight": weight})
    assert athlete.put(f"/coaching-requests/{coach.id}", body)[0] == 200


def test_request_accept_and_lists(make_user):
    c, a = make_user("coach"), make_user("athlete")
    code, body = c.post("/coaching-requests", {"username": "ATHLETE"})
    assert (code, body["status"]) == (201, "pending")
    assert [e["athlete"]["username"] for e in a.get("/coaching")[1]["incoming"]] == ["coach"]
    assert [e["athlete"]["username"] for e in c.get("/coaching")[1]["outgoing"]] == ["athlete"]
    a.put(f"/coaching-requests/{c.id}", {"status": "accepted"})
    assert [e["athlete"]["username"] for e in a.get("/coaching")[1]["coaches"]] == ["coach"]
    assert [e["athlete"]["username"] for e in c.get("/coaching")[1]["athletes"]] == ["athlete"]


def test_rules(make_user):
    c, a = make_user("coach"), make_user("athlete")
    assert c.post("/coaching-requests", {"username": "coach"})[0] == 400
    assert c.post("/coaching-requests", {"username": "nobody"})[0] == 404
    c.post("/coaching-requests", {"username": "athlete"})
    assert c.post("/coaching-requests", {"username": "athlete"})[0] == 409
    assert c.put(f"/coaching-requests/{a.id}", {"status": "accepted"})[0] == 404  # only the athlete answers
    assert a.put(f"/coaching-requests/{c.id}", {"status": "maybe"})[0] == 400
    a.put(f"/coaching-requests/{c.id}", {"status": "declined"})
    assert a.put(f"/coaching-requests/{c.id}", {"status": "accepted"})[0] == 409
    # A declined request still looks pending to the coach.
    assert [e["athlete"]["username"] for e in c.get("/coaching")[1]["outgoing"]] == ["athlete"]
    assert c.post("/coaching-requests", {"username": "athlete"})[0] == 409


def test_no_access_until_accepted(make_user):
    c, a = make_user("coach"), make_user("athlete")
    lift_id = add_lift(a)
    c.post("/coaching-requests", {"username": "athlete"})
    assert c.get(f"/coached/{a.id}/lifts")[0] == 404
    assert c.get(f"/coached/{a.id}/lifts/{lift_id}")[0] == 404
    assert c.get(f"/coached/{a.id}")[0] == 404


def test_coach_reads_lifts_and_programs_without_body_weight(make_user):
    c, a, stranger = make_user("coach"), make_user("athlete"), make_user("stranger")
    lift_id, pid = add_lift(a), add_program(a)
    link(c, a)
    code, lifts = c.get(f"/coached/{a.id}/lifts")
    assert code == 200 and lifts == [{"id": lift_id, "date": lifts[0]["date"]}]
    code, lift = c.get(f"/coached/{a.id}/lifts/{lift_id}")
    assert code == 200 and "body_weight" not in lift and lift["sets"][0]["subsets"][0]["weight"] == 225
    assert c.get(f"/coached/{a.id}/programs")[1][0]["id"] == pid
    assert c.get(f"/coached/{a.id}/programs/{pid}")[0] == 200
    profile = c.get(f"/coached/{a.id}")[1]
    assert not {"dob", "gender", "height", "cognito_sub"} & profile.keys()
    assert stranger.get(f"/coached/{a.id}/lifts")[0] == 404
    # Read-only: there is no route to change the athlete's data.
    assert c.put(f"/coached/{a.id}/lifts/{lift_id}", {"date": "2026-02-01"})[0] == 405


def test_body_weight_only_when_allowed_and_revocable(make_user):
    c, a = make_user("coach"), make_user("athlete")
    lift_id = add_lift(a)
    link(c, a, weight=True)
    assert c.get(f"/coached/{a.id}/lifts/{lift_id}")[1]["body_weight"] == 180
    assert c.get(f"/coached/{a.id}/lifts")[1][0]["body_weight"] == 180
    assert a.put(f"/coaches/{c.id}", {"can_see_body_weight": False})[0] == 200
    assert "body_weight" not in c.get(f"/coached/{a.id}/lifts/{lift_id}")[1]
    assert c.put(f"/coaches/{a.id}", {"can_see_body_weight": True})[0] == 404  # coach can't grant it


def test_either_side_can_end_it(make_user):
    c, a = make_user("coach"), make_user("athlete")
    link(c, a)
    assert a.delete(f"/coaches/{c.id}")[0] == 200
    assert c.get(f"/coached/{a.id}/lifts")[0] == 404
    link(c, a)  # row is gone, so it can be asked again
    assert c.delete(f"/coached/{a.id}")[0] == 200
    assert a.get("/coaching")[1]["coaches"] == []


def test_coach_can_withdraw_a_request(make_user):
    c, a = make_user("coach"), make_user("athlete")
    c.post("/coaching-requests", {"username": "athlete"})
    assert c.delete(f"/coaching-requests/{a.id}")[0] == 200
    assert a.get("/coaching")[1]["incoming"] == []


def test_blocking_ends_coaching_and_stops_requests(make_user):
    c, a = make_user("coach"), make_user("athlete")
    link(c, a)
    assert a.put(f"/blocks/{c.id}")[0] == 200
    assert c.get(f"/coached/{a.id}/lifts")[0] == 404
    assert c.post("/coaching-requests", {"username": "athlete"}) == (404, {"error": "No athlete with that username"})


def test_friendship_alone_gives_a_coach_nothing(make_user):
    c, a = make_user("coach"), make_user("athlete")
    befriend(c, a)
    assert c.get(f"/coached/{a.id}/lifts")[0] == 404


def test_export_and_delete_cover_coaching(make_user, sql):
    c, a = make_user("coach"), make_user("athlete")
    link(c, a, weight=True)
    assert a.get("/me/export")[1]["coaches"] == [
        {"username": "coach", "first_name": None, "last_name": None, "can_see_body_weight": True}]
    assert [x["username"] for x in c.get("/me/export")[1]["coaching"]] == ["athlete"]
    assert a.delete("/me", {"confirm": "athlete"})[0] == 200
    assert sql("SELECT COUNT(*) FROM workout.coaching")[0][0] == 0
    assert c.get("/coaching")[1]["athletes"] == []
