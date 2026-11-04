"""Coach groups: private to the coach, only athletes they coach, cleaned up with the link."""

from .conftest import add_program
from .test_coaching import link


def groups(coach):
    return coach.get("/coaching")[1]["groups"]


def coach_with(make_user, n=2):
    coach = make_user("coach")
    athletes = [make_user(f"athlete{i}") for i in range(n)]
    for a in athletes:
        link(coach, a)
    return coach, athletes


def test_create_rename_fill_and_delete(make_user):
    coach, (a1, a2) = coach_with(make_user)
    code, g = coach.post("/coach-groups", {"name": "Powerlifting", "athlete_ids": [a1.id]})
    assert code == 201 and g["athlete_ids"] == [a1.id]
    assert coach.put(f"/coach-groups/{g['id']}", {"athlete_ids": [a2.id, a1.id, a2.id]})[1]["athlete_ids"] == sorted([a1.id, a2.id])
    assert coach.put(f"/coach-groups/{g['id']}", {"name": "Lifters"})[1]["name"] == "Lifters"
    assert groups(coach) == [{"id": g["id"], "name": "Lifters", "athlete_ids": sorted([a1.id, a2.id])}]
    assert coach.put(f"/coach-groups/{g['id']}", {"athlete_ids": []})[1]["athlete_ids"] == []
    assert coach.delete(f"/coach-groups/{g['id']}")[0] == 200
    assert groups(coach) == []
    assert coach.get("/coaching")[1]["athletes"][0]["athlete"]["username"] == "athlete0"  # athletes stay


def test_an_athlete_can_be_in_several_groups(make_user):
    coach, (a1, a2) = coach_with(make_user)
    coach.post("/coach-groups", {"name": "A", "athlete_ids": [a1.id, a2.id]})
    coach.post("/coach-groups", {"name": "B", "athlete_ids": [a1.id]})
    assert [(g["name"], len(g["athlete_ids"])) for g in groups(coach)] == [("A", 2), ("B", 1)]


def test_rules(make_user, sql):
    coach, (a1, _) = coach_with(make_user)
    stranger = make_user("stranger")
    assert coach.post("/coach-groups", {"name": "  "})[0] == 400
    assert coach.post("/coach-groups", {"name": "x" * 41})[0] == 400
    assert coach.post("/coach-groups", {"name": "G", "athlete_ids": [stranger.id]})[0] == 400
    assert coach.post("/coach-groups", {"name": "G", "athlete_ids": ["1"]})[0] == 400
    gid = coach.post("/coach-groups", {"name": "G"})[1]["id"]
    assert coach.post("/coach-groups", {"name": "g"})[0] == 409  # names are unique, ignoring case
    assert coach.put(f"/coach-groups/{gid}", {})[0] == 400
    assert coach.put(f"/coach-groups/{gid}", {"athlete_ids": [stranger.id]})[0] == 400
    for i in range(19):  # fill up to the limit directly, since creating them also counts as attempts
        sql("INSERT INTO workout.coach_group (coach_id, name) VALUES (:c, :n)", c=coach.id, n=f"group {i}")
    assert coach.post("/coach-groups", {"name": "one too many"})[0] == 422


def test_groups_are_private_to_their_coach(make_user):
    coach, (a1, _) = coach_with(make_user)
    other = make_user("othercoach")
    link(other, a1)
    gid = coach.post("/coach-groups", {"name": "Mine", "athlete_ids": [a1.id]})[1]["id"]
    assert groups(other) == []
    assert other.put(f"/coach-groups/{gid}", {"name": "Stolen"})[0] == 404
    assert other.delete(f"/coach-groups/{gid}")[0] == 404
    assert "groups" in a1.get("/coaching")[1] and a1.get("/coaching")[1]["groups"] == []  # athlete has none


def test_membership_ends_with_the_coaching_link(make_user):
    for how in ("coach", "athlete", "block"):
        coach = make_user(f"c-{how}")
        athlete = make_user(f"a-{how}")
        link(coach, athlete)
        gid = coach.post("/coach-groups", {"name": "G", "athlete_ids": [athlete.id]})[1]["id"]
        if how == "coach":
            coach.delete(f"/coached/{athlete.id}")
        elif how == "athlete":
            athlete.delete(f"/coaches/{coach.id}")
        else:
            athlete.put(f"/blocks/{coach.id}")
        assert groups(coach) == [{"id": gid, "name": "G", "athlete_ids": []}]
        link(coach, athlete) if how != "block" else None
        if how != "block":
            assert groups(coach)[0]["athlete_ids"] == []  # a new link does not bring the old group back


def test_group_members_can_be_sent_a_program(make_user):
    coach, (a1, a2) = coach_with(make_user)
    coach.post("/coach-groups", {"name": "G", "athlete_ids": [a1.id]})
    ids = groups(coach)[0]["athlete_ids"]
    pid = add_program(coach)
    assert all(r["ok"] for r in coach.post(f"/programs/{pid}/send", {"athlete_ids": ids})[1]["results"])
    assert len(a1.get("/programs")[1]) == 1 and a2.get("/programs")[1] == []


def test_export_and_delete(make_user, sql):
    coach, (a1, a2) = coach_with(make_user)
    coach.post("/coach-groups", {"name": "G", "athlete_ids": [a1.id, a2.id]})
    assert coach.get("/me/export")[1]["coach_groups"] == [{"name": "G", "athletes": ["athlete0", "athlete1"]}]
    assert a1.get("/me/export")[1].get("coach_groups") == []  # an athlete has none and sees none of the coach's
    assert a1.delete("/me", {"confirm": "athlete0"})[0] == 200
    assert groups(coach)[0]["athlete_ids"] == [a2.id]
    assert coach.delete("/me", {"confirm": "coach"})[0] == 200
    assert sql("SELECT COUNT(*) FROM workout.coach_group")[0][0] == 0
    assert sql("SELECT COUNT(*) FROM workout.coach_group_member")[0][0] == 0
