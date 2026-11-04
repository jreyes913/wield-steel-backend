"""A program a coach sends is kept in sync, read-only and private for the athlete."""

from .conftest import add_lift, add_program, befriend
from .test_coaching import link


def setup(make_user, sets=1, tag=""):
    coach, athlete = make_user("coach" + tag), make_user("athlete" + tag)
    link(coach, athlete)
    pid = add_program(coach, sets=sets, public=False)
    code, body = coach.post(f"/programs/{pid}/send", {"athlete_ids": [athlete.id]})
    assert code == 200 and body["results"][0]["ok"]
    return coach, athlete, pid, body["results"][0]["program_id"]


def shape(user, program_id):
    """[sets in each day] of a program as the user sees it."""
    return [len(d["sets"]) for d in user.get(f"/programs/{program_id}")[1]["days"]]


def test_athlete_sees_every_change_the_coach_makes(make_user):
    coach, athlete, pid, mine = setup(make_user, sets=2)
    assert shape(athlete, mine) == [2]
    day = coach.get(f"/programs/{pid}")[1]["days"][0]
    # A set is added, then one deleted, a day added, the program renamed.
    code, new_set = coach.post(f"/program-days/{day['id']}/sets", {"position": 9, "subsets": [{"exercise_id": 1, "position": 1, "reps": 5}]})
    assert shape(athlete, mine) == [3]
    coach.delete(f"/program-sets/{new_set['id']}")
    coach.delete(f"/program-sets/{day['sets'][0]['id']}")
    assert shape(athlete, mine) == [1]
    coach.post(f"/programs/{pid}/days", {"name": "Day 2"})
    assert shape(athlete, mine) == [1, 0]
    coach.put(f"/programs/{pid}", {"name": "Renamed", "start_date": "2026-03-01"})
    listed = athlete.get("/programs")[1]
    assert [(p["id"], p["name"], p["start_date"], p["synced"]) for p in listed] == [(mine, "Renamed", "2026-03-01", True)]
    assert listed[0]["assigned_by_name"] == "coach"
    assert athlete.get(f"/programs/{mine}")[1]["name"] == "Renamed"


def test_it_is_read_only_and_permanently_private_for_the_athlete(make_user):
    coach, athlete, pid, mine = setup(make_user)
    assert athlete.put(f"/programs/{mine}", {"name": "Mine"})[0] == 409
    assert athlete.put(f"/programs/{mine}", {"is_public": True})[0] == 409
    assert athlete.post(f"/programs/{mine}/days", {"name": "Extra"})[0] == 409
    coach_day = coach.get(f"/programs/{pid}")[1]["days"][0]
    # The coach's days and sets are theirs: the athlete cannot touch them by id.
    assert athlete.put(f"/program-days/{coach_day['id']}", {"name": "x"})[0] == 404
    assert athlete.delete(f"/program-sets/{coach_day['sets'][0]['id']}")[0] == 404
    assert athlete.post(f"/program-days/{coach_day['id']}/sets", {"position": 5})[0] == 404
    assert shape(coach, pid) == [1]
    # Not shown to friends or coaches of the athlete, and cannot be sent on.
    other = make_user("other")
    befriend(athlete, other)
    assert other.get(f"/athletes/{athlete.id}/programs")[1] == []
    assert athlete.post(f"/programs/{mine}/send", {"athlete_ids": [coach.id]})[0] == 400


def test_athlete_can_use_it_for_a_lift_and_can_delete_it(make_user):
    coach, athlete, pid, mine = setup(make_user, sets=2)
    day_id = athlete.get(f"/programs/{mine}")[1]["days"][0]["id"]
    lift_id = add_lift(athlete)
    assert athlete.post(f"/lifts/{lift_id}/apply", {"program_day_id": day_id})[0] == 201
    assert len(athlete.get(f"/lifts/{lift_id}")[1]["sets"]) == 3  # 1 logged + 2 from the program
    assert athlete.delete(f"/programs/{mine}")[0] == 200
    assert len(coach.get(f"/programs/{pid}")[1]["days"][0]["sets"]) == 2  # the coach's is untouched
    # Someone who never followed it cannot apply the coach's day.
    stranger = make_user("stranger")
    assert stranger.post(f"/lifts/{add_lift(stranger)}/apply", {"program_day_id": day_id})[0] == 404


def test_sending_twice_does_not_duplicate(make_user):
    coach, athlete, pid, mine = setup(make_user)
    body = coach.post(f"/programs/{pid}/send", {"athlete_ids": [athlete.id]})[1]["results"][0]
    assert (body["program_id"], body["already_sent"]) == (mine, True)
    assert len(athlete.get("/programs")[1]) == 1


def test_deleting_the_program_leaves_the_athlete_a_frozen_private_copy(make_user):
    coach, athlete, pid, mine = setup(make_user, sets=2)
    coach.delete(f"/programs/{pid}")
    [kept] = athlete.get("/programs")[1]
    assert kept["id"] == mine and kept["name"] == "Push" and kept["synced"] is False
    assert shape(athlete, mine) == [2]
    assert athlete.put(f"/programs/{mine}", {"name": "My own"})[0] == 200  # now theirs to edit
    assert athlete.put(f"/programs/{mine}", {"is_public": True})[0] == 400  # but never public
    assert athlete.post(f"/programs/{mine}/send", {"athlete_ids": [coach.id]})[0] == 400


def test_ending_coaching_freezes_what_was_sent(make_user):
    coach, athlete, pid, mine = setup(make_user, sets=2)
    athlete.delete(f"/coaches/{coach.id}")
    day = coach.get(f"/programs/{pid}")[1]["days"][0]
    coach.post(f"/program-days/{day['id']}/sets", {"position": 7})  # no longer reaches the athlete
    assert shape(athlete, mine) == [2]
    assert athlete.get("/programs")[1][0]["synced"] is False


def test_coach_stopping_and_blocking_also_freeze(make_user):
    coach, athlete, pid, mine = setup(make_user)
    coach.delete(f"/coached/{athlete.id}")
    assert athlete.get("/programs")[1][0]["synced"] is False
    coach2, athlete2, pid2, mine2 = setup(make_user, tag="2")
    athlete2.put(f"/blocks/{coach2.id}")
    assert athlete2.get("/programs")[1][0]["synced"] is False
    assert shape(athlete2, mine2) == [1]


def test_deleting_the_coach_account_freezes_athletes_copies(make_user):
    coach, athlete, pid, mine = setup(make_user, sets=2)
    assert coach.delete("/me", {"confirm": "coach"})[0] == 200
    [kept] = athlete.get("/programs")[1]
    assert kept["synced"] is False and shape(athlete, mine) == [2]


def test_deleting_the_athlete_account_leaves_the_coach_alone(make_user):
    coach, athlete, pid, mine = setup(make_user)
    assert athlete.delete("/me", {"confirm": "athlete"})[0] == 200
    assert shape(coach, pid) == [1]


def test_friend_copy_is_a_clean_copy_with_no_syncing(make_user):
    owner, friend = make_user("owner"), make_user("friend")
    befriend(owner, friend)
    pid = add_program(owner, sets=2, public=True)
    copy = friend.post(f"/athletes/{owner.id}/programs/{pid}/copy")[1]
    owner.post(f"/program-days/{owner.get(f'/programs/{pid}')[1]['days'][0]['id']}/sets", {"position": 9})
    assert shape(friend, copy["id"]) == [2]  # unchanged
    mine = friend.get("/programs")[1][0]
    assert mine["synced"] is False and mine["assigned_by"] is None
    assert friend.put(f"/programs/{copy['id']}", {"is_public": True})[0] == 200  # a clean copy can be shared


def test_export_includes_the_synced_content(make_user):
    coach, athlete, pid, mine = setup(make_user, sets=2)
    [prog] = athlete.get("/me/export")[1]["programs"]
    assert prog["synced_with_coach"] is True and prog["public"] is False
    assert [len(d["sets"]) for d in prog["days"]] == [2]
