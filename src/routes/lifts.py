"""Logged workouts: lifts, their sets and subsets, and applying a program day."""

from __future__ import annotations

from ..auth import own_lift, own_program_day, own_set, own_subset
from ..cascade import delete_lift_tree, delete_set_tree
from ..db import Tx, query, transaction
from ..responses import respond
from ..rows import assert_exists, update_row
from ..validation import (
    optional, path_id, require_fields, to_date, to_float, to_int, validate_subsets,
)

SUBSET_FIELDS = {
    "exercise_id": (to_int, True),
    "position":    (to_int, True),
    "reps":        (to_int, False),
    "rir":         (to_int, False),
    "weight":      (to_float, False),
}


def _lift_detail(lift: dict) -> dict:
    """A lift with its sets and subsets nested, in position order."""
    rows = query(
        """SELECT s.id AS set_id, s.position AS set_position,
                  sub.id AS subset_id, sub.position AS subset_position,
                  sub.reps, sub.rir, sub.weight,
                  e.id AS exercise_id, e.name AS exercise_name
             FROM workout.exercise_set s
             LEFT JOIN workout.exercise_subset sub ON sub.exercise_set_id = s.id
             LEFT JOIN workout.exercise e ON e.id = sub.exercise_id
            WHERE s.lift_id = :id
            ORDER BY s.position, s.id, sub.position, sub.id""",
        id=lift["id"],
    )
    sets: dict[int, dict] = {}
    for r in rows:
        s = sets.setdefault(
            r["set_id"], {"id": r["set_id"], "position": r["set_position"], "subsets": []}
        )
        if r["subset_id"] is not None:
            s["subsets"].append({
                "id": r["subset_id"],
                "position": r["subset_position"],
                "exercise": {"id": r["exercise_id"], "name": r["exercise_name"]},
                "reps": r["reps"],
                "rir": r["rir"],
                "weight": r["weight"],
            })
    return {**lift, "sets": list(sets.values())}


# ---------- Lifts ----------

def list_my_lifts(req):
    rows = query(
        "SELECT * FROM workout.lift WHERE athlete_id = :me ORDER BY date DESC, id DESC",
        me=req["athlete_id"],
    )
    return respond(200, rows)


def create_lift(req):
    body = req["body"]
    require_fields(body, "date")
    [row] = query(
        """INSERT INTO workout.lift (date, athlete_id, body_weight)
           VALUES (:date, :me, :bw) RETURNING *""",
        date=to_date(body["date"]),
        me=req["athlete_id"],  # always the caller — never taken from the body
        bw=optional(to_int, body.get("body_weight"), "body_weight"),
    )
    return respond(201, row)


def get_lift(req):
    lift = own_lift(path_id(req), req["athlete_id"])
    return respond(200, _lift_detail(lift))


def update_lift(req):
    lift_id = path_id(req)
    own_lift(lift_id, req["athlete_id"])
    row = update_row("lift", lift_id, req["body"], {
        "date":        (to_date, True),
        "body_weight": (to_int, False),
    })
    return respond(200, row)


def delete_lift(req):
    lift_id = path_id(req)

    def work(tx: Tx):
        own_lift(lift_id, req["athlete_id"], tx)
        delete_lift_tree(tx, lift_id)

    transaction(work)
    return respond(200, {"deleted": lift_id})


# ---------- Sets and subsets in a lift ----------

def _insert_subset(tx: Tx, set_id: int, sub: dict) -> dict:
    exercise_id = to_int(sub["exercise_id"], "exercise_id")
    assert_exists("exercise", exercise_id, tx)
    return tx.query(
        """INSERT INTO workout.exercise_subset
             (exercise_set_id, exercise_id, position, reps, rir, weight)
           VALUES (:set_id, :ex, :pos, :reps, :rir, :weight) RETURNING *""",
        set_id=set_id,
        ex=exercise_id,
        pos=to_int(sub["position"], "position"),
        reps=optional(to_int, sub.get("reps"), "reps"),
        rir=optional(to_int, sub.get("rir"), "rir"),
        weight=optional(to_float, sub.get("weight"), "weight"),
    )[0]


def add_set_to_lift(req):
    lift_id = path_id(req)
    body = req["body"]
    require_fields(body, "position")
    position = to_int(body["position"], "position")
    subsets = validate_subsets(body.get("subsets"))

    def work(tx: Tx):
        own_lift(lift_id, req["athlete_id"], tx)
        [new_set] = tx.query(
            """INSERT INTO workout.exercise_set (lift_id, position)
               VALUES (:lift_id, :pos) RETURNING *""",
            lift_id=lift_id,
            pos=position,
        )
        saved = [_insert_subset(tx, new_set["id"], sub) for sub in subsets]
        return {**new_set, "subsets": saved}

    return respond(201, transaction(work))


def update_set(req):
    set_id = path_id(req)
    own_set(set_id, req["athlete_id"])
    row = update_row("exercise_set", set_id, req["body"], {"position": (to_int, True)})
    return respond(200, row)


def delete_set(req):
    set_id = path_id(req)

    def work(tx: Tx):
        own_set(set_id, req["athlete_id"], tx)
        delete_set_tree(tx, set_id)

    transaction(work)
    return respond(200, {"deleted": set_id})


def add_subset_to_set(req):
    set_id = path_id(req)
    body = req["body"]
    require_fields(body, "exercise_id", "position")

    def work(tx: Tx):
        own_set(set_id, req["athlete_id"], tx)
        return _insert_subset(tx, set_id, body)

    return respond(201, transaction(work))


def update_subset(req):
    subset_id = path_id(req)
    body = req["body"]
    own_subset(subset_id, req["athlete_id"])
    if body.get("exercise_id") is not None:
        assert_exists("exercise", to_int(body["exercise_id"], "exercise_id"))
    return respond(200, update_row("exercise_subset", subset_id, body, SUBSET_FIELDS))


def delete_subset(req):
    subset_id = path_id(req)
    own_subset(subset_id, req["athlete_id"])
    query("DELETE FROM workout.exercise_subset WHERE id = :id", id=subset_id)
    return respond(200, {"deleted": subset_id})


# ---------- Apply a program day to a lift ----------

def apply_program_day(req):
    lift_id = path_id(req)
    body = req["body"]
    require_fields(body, "program_day_id")
    day_id = to_int(body["program_day_id"], "program_day_id")
    me = req["athlete_id"]

    def work(tx: Tx):
        lift = own_lift(lift_id, me, tx)
        own_program_day(day_id, me, tx)

        # Append after any sets already in the lift so positions don't collide.
        max_pos = tx.query(
            "SELECT COALESCE(MAX(position), 0) AS max_pos FROM workout.exercise_set WHERE lift_id = :id",
            id=lift_id,
        )[0]["max_pos"]
        template_sets = tx.query(
            """SELECT id FROM workout.program_exercise_set
                WHERE program_lift_id = :id ORDER BY position, id""",
            id=day_id,
        )
        for i, tset in enumerate(template_sets, start=1):
            [new_set] = tx.query(
                """INSERT INTO workout.exercise_set (lift_id, position)
                   VALUES (:lift_id, :pos) RETURNING id""",
                lift_id=lift_id,
                pos=max_pos + i,
            )
            for tsub in tx.query(
                """SELECT exercise_id, position, reps FROM workout.program_exercise_subset
                    WHERE program_exercise_set_id = :id ORDER BY position, id""",
                id=tset["id"],
            ):
                # Reps come from the plan; weight and RIR are left empty to fill in.
                tx.query(
                    """INSERT INTO workout.exercise_subset
                         (exercise_set_id, exercise_id, position, reps, rir, weight)
                       VALUES (:set_id, :ex, :pos, :reps, NULL, NULL)""",
                    set_id=new_set["id"],
                    ex=tsub["exercise_id"],
                    pos=tsub["position"],
                    reps=tsub["reps"],
                )
        return lift

    lift = transaction(work)
    return respond(201, _lift_detail(lift))
