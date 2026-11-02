"""Copying a friend's program: the copy is independent, and oversized programs are refused."""

from src.config import PROGRAM_COPY_ROW_LIMIT

from .conftest import add_program, befriend


def counts(sql):
    return {t: sql(f"SELECT COUNT(*) FROM workout.{t}")[0][0] for t in (
        "program", "program_lift", "program_exercise_set", "program_exercise_subset")}


def test_copy_is_independent(make_user):
    o, me = make_user("owner"), make_user("copier")
    befriend(me, o)
    pid = add_program(o, sets=2)
    code, copy = me.post(f"/athletes/{o.id}/programs/{pid}/copy")
    assert code == 201 and copy["id"] != pid
    assert copy["name"] == "Push (from owner)"
    assert [len(d["sets"]) for d in copy["days"]] == [2]
    assert [p["id"] for p in me.get("/programs")[1]] == [copy["id"]]
    # Editing or deleting the copy leaves the original alone, and vice versa.
    day_id = copy["days"][0]["id"]
    me.put(f"/program-days/{day_id}", {"name": "Changed"})
    me.delete(f"/programs/{copy['id']}")
    original = o.get(f"/programs/{pid}")[1]
    assert original["days"][0]["name"] == "Day 1" and len(original["days"][0]["sets"]) == 2
    o.put(f"/programs/{pid}", {"name": "Renamed"})
    assert me.get("/programs")[1] == []


def test_non_friend_cannot_copy(make_user, sql):
    o, stranger = make_user("owner"), make_user("stranger")
    pid = add_program(o)
    before = counts(sql)
    assert stranger.post(f"/athletes/{o.id}/programs/{pid}/copy")[0] == 404
    assert counts(sql) == before


def test_over_the_limit_is_422_and_writes_nothing(make_user, sql):
    o, me = make_user("owner"), make_user("copier")
    befriend(me, o)
    pid = add_program(o)
    # One day + 1 set + 1 subset = 3 rows; add enough sets to pass the limit.
    [day_id] = sql(f"SELECT id FROM workout.program_lift WHERE program_id = {pid}")[0]
    sql("WITH RECURSIVE n(i) AS (SELECT 2 UNION ALL SELECT i + 1 FROM n WHERE i < :n) "
        "INSERT INTO workout.program_exercise_set (program_lift_id, position) SELECT :d, i FROM n",
        n=PROGRAM_COPY_ROW_LIMIT, d=day_id)
    before = counts(sql)
    code, body = me.post(f"/athletes/{o.id}/programs/{pid}/copy")
    assert code == 422 and "too big to copy" in body["error"]
    assert counts(sql) == before
