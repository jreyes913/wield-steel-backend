"""Programs (templates). A program has days, days have sets, sets have subsets."""

from __future__ import annotations

from ..auth import (
    editable_program, own_program, own_program_day, own_program_set, own_program_subset,
)
from ..cascade import delete_program_day_tree, delete_program_set_tree, delete_program_tree
from ..db import Tx, query, transaction
from ..program_copy import detach_copies_of
from ..responses import HttpError
from ..responses import respond
from ..rows import assert_exists, update_row
from ..validation import (
    optional, path_id, require_fields, to_bool, to_date, to_int, to_text, validate_subsets,
)

PROGRAM_FIELDS = {
    "name":       (to_text, True),
    "start_date": (to_date, False),
    "end_date":   (to_date, False),
    "is_public":  (to_bool, True),  # visible to my friends and my coaches; private by default
}

PROGRAM_SUBSET_FIELDS = {
    "exercise_id": (to_int, True),
    "position":    (to_int, True),
    "reps":        (to_int, False),
}


def _program_detail(program: dict) -> dict:
    """A program with its days, sets and subsets nested, in order."""
    rows = query(
        """SELECT pl.id AS day_id, pl.name AS day_name,
                  ps.id AS set_id, ps.position AS set_position,
                  pss.id AS subset_id, pss.position AS subset_position, pss.reps,
                  e.id AS exercise_id, e.name AS exercise_name
             FROM workout.program_lift pl
             LEFT JOIN workout.program_exercise_set ps ON ps.program_lift_id = pl.id
             LEFT JOIN workout.program_exercise_subset pss ON pss.program_exercise_set_id = ps.id
             LEFT JOIN workout.exercise e ON e.id = pss.exercise_id
            WHERE pl.program_id = :id
            ORDER BY pl.id, ps.position, ps.id, pss.position, pss.id""",
        id=program["id"],
    )
    days: dict[int, dict] = {}
    sets: dict[int, dict] = {}
    for r in rows:
        day = days.setdefault(r["day_id"], {"id": r["day_id"], "name": r["day_name"], "sets": []})
        if r["set_id"] is None:
            continue
        if r["set_id"] not in sets:
            sets[r["set_id"]] = {"id": r["set_id"], "position": r["set_position"], "subsets": []}
            day["sets"].append(sets[r["set_id"]])
        if r["subset_id"] is not None:
            sets[r["set_id"]]["subsets"].append({
                "id": r["subset_id"],
                "position": r["subset_position"],
                "exercise": {"id": r["exercise_id"], "name": r["exercise_name"]},
                "reps": r["reps"],
            })
    return {**program, "days": list(days.values())}


# ---------- Programs ----------

def _sender_names(rows: list[dict]) -> dict:
    """Display names of the coaches who sent these programs (missing if they have left)."""
    names = {}
    for sender in {r["assigned_by"] for r in rows if r["assigned_by"] is not None}:
        found = query(
            "SELECT username, first_name, last_name FROM workout.athlete WHERE id = :id", id=sender
        )
        if found:
            f = found[0]
            names[sender] = " ".join(p for p in (f["first_name"], f["last_name"]) if p) or f["username"]
    return names


def _followed(program: dict) -> dict:
    """The coach's program that a synced program shows (its name, dates and days come from it)."""
    rows = query(
        "SELECT id, name, start_date, end_date FROM workout.program WHERE id = :id",
        id=program["source_program_id"],
    )
    if not rows:  # cannot happen: the sync is ended before the coach's program goes
        raise HttpError(404, "Program not found")
    return rows[0]


def _as_synced(program: dict, followed: dict) -> dict:
    """My row, showing the coach's name and dates. Always private and read-only."""
    return {**program, "name": followed["name"], "start_date": followed["start_date"],
            "end_date": followed["end_date"], "is_public": False, "synced": True}


def list_programs(req):
    rows = query(
        "SELECT * FROM workout.program WHERE athlete_id = :me ORDER BY id DESC",
        me=req["athlete_id"],
    )
    names = _sender_names(rows)
    out = []
    for r in rows:
        r = _as_synced(r, _followed(r)) if r["source_program_id"] is not None else {**r, "synced": False}
        r["is_public"] = bool(r["is_public"])
        r["assigned_by_name"] = names.get(r["assigned_by"])
        out.append(r)
    return respond(200, out)


def create_program(req):
    body = req["body"]
    require_fields(body, "name")
    [row] = query(
        """INSERT INTO workout.program (athlete_id, name, start_date, end_date, is_public)
           VALUES (:me, :name, :start, :end, :public) RETURNING *""",
        me=req["athlete_id"],
        public=optional(to_bool, body.get("is_public"), "is_public") or False,
        name=to_text(body["name"], "name"),
        start=optional(to_date, body.get("start_date"), "start_date"),
        end=optional(to_date, body.get("end_date"), "end_date"),
    )
    return respond(201, row)


def get_program(req):
    program = own_program(path_id(req), req["athlete_id"])
    if program["source_program_id"] is None:
        return respond(200, {**_program_detail(program), "synced": False})
    # A coach's program I follow: their days (so their ids), my row's id.
    followed = _followed(program)
    detail = _program_detail(followed)
    return respond(200, {**_as_synced(program, followed), "days": detail["days"],
                         "assigned_by_name": _sender_names([program]).get(program["assigned_by"])})


def update_program(req):
    program_id = path_id(req)
    program = editable_program(program_id, req["athlete_id"])
    if program["assigned_by"] is not None and req["body"].get("is_public"):
        # Whatever a coach sent stays private, even once it is no longer kept in sync.
        raise HttpError(400, "A program from a coach can't be made public")
    return respond(200, update_row("program", program_id, req["body"], PROGRAM_FIELDS))


def delete_program(req):
    program_id = path_id(req)
    own_program(program_id, req["athlete_id"])
    # Athletes who follow this program keep what they have, as an ordinary private copy.
    detach_copies_of(program_id)

    def work(tx: Tx):
        own_program(program_id, req["athlete_id"], tx)
        delete_program_tree(tx, program_id)

    transaction(work)
    return respond(200, {"deleted": program_id})


# ---------- Program days ----------

def add_program_day(req):
    program_id = path_id(req)
    body = req["body"]
    require_fields(body, "name")
    editable_program(program_id, req["athlete_id"])
    [row] = query(
        """INSERT INTO workout.program_lift (program_id, name)
           VALUES (:pid, :name) RETURNING *""",
        pid=program_id,
        name=to_text(body["name"], "name"),
    )
    return respond(201, row)


def update_program_day(req):
    day_id = path_id(req)
    own_program_day(day_id, req["athlete_id"])
    return respond(200, update_row("program_lift", day_id, req["body"], {"name": (to_text, True)}))


def delete_program_day(req):
    day_id = path_id(req)

    def work(tx: Tx):
        own_program_day(day_id, req["athlete_id"], tx)
        delete_program_day_tree(tx, day_id)

    transaction(work)
    return respond(200, {"deleted": day_id})


# ---------- Program sets and subsets ----------

def _insert_program_subset(tx: Tx, set_id: int, sub: dict) -> dict:
    exercise_id = to_int(sub["exercise_id"], "exercise_id")
    assert_exists("exercise", exercise_id, tx)
    return tx.query(
        """INSERT INTO workout.program_exercise_subset
             (program_exercise_set_id, exercise_id, position, reps)
           VALUES (:set_id, :ex, :pos, :reps) RETURNING *""",
        set_id=set_id,
        ex=exercise_id,
        pos=to_int(sub["position"], "position"),
        reps=optional(to_int, sub.get("reps"), "reps"),
    )[0]


def add_program_set(req):
    day_id = path_id(req)
    body = req["body"]
    require_fields(body, "position")
    position = to_int(body["position"], "position")
    subsets = validate_subsets(body.get("subsets"))

    def work(tx: Tx):
        own_program_day(day_id, req["athlete_id"], tx)
        [new_set] = tx.query(
            """INSERT INTO workout.program_exercise_set (program_lift_id, position)
               VALUES (:day_id, :pos) RETURNING *""",
            day_id=day_id,
            pos=position,
        )
        saved = [_insert_program_subset(tx, new_set["id"], sub) for sub in subsets]
        return {**new_set, "subsets": saved}

    return respond(201, transaction(work))


def update_program_set(req):
    set_id = path_id(req)
    own_program_set(set_id, req["athlete_id"])
    return respond(200, update_row("program_exercise_set", set_id, req["body"], {"position": (to_int, True)}))


def delete_program_set(req):
    set_id = path_id(req)

    def work(tx: Tx):
        own_program_set(set_id, req["athlete_id"], tx)
        delete_program_set_tree(tx, set_id)

    transaction(work)
    return respond(200, {"deleted": set_id})


def add_program_subset(req):
    set_id = path_id(req)
    body = req["body"]
    require_fields(body, "exercise_id", "position")

    def work(tx: Tx):
        own_program_set(set_id, req["athlete_id"], tx)
        return _insert_program_subset(tx, set_id, body)

    return respond(201, transaction(work))


def update_program_subset(req):
    subset_id = path_id(req)
    body = req["body"]
    own_program_subset(subset_id, req["athlete_id"])
    if body.get("exercise_id") is not None:
        assert_exists("exercise", to_int(body["exercise_id"], "exercise_id"))
    return respond(200, update_row("program_exercise_subset", subset_id, body, PROGRAM_SUBSET_FIELDS))


def delete_program_subset(req):
    subset_id = path_id(req)
    own_program_subset(subset_id, req["athlete_id"])
    query("DELETE FROM workout.program_exercise_subset WHERE id = :id", id=subset_id)
    return respond(200, {"deleted": subset_id})
