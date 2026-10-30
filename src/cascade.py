"""Child-first deletes (DSQL has no ON DELETE CASCADE).

Each helper deletes one parent and everything under it, using single-id
statements. Call them inside transaction() so it's all or nothing.
"""

from __future__ import annotations

from .db import Tx


def delete_set_tree(tx: Tx, set_id: int) -> None:
    tx.query("DELETE FROM workout.exercise_subset WHERE exercise_set_id = :id", id=set_id)
    tx.query("DELETE FROM workout.exercise_set WHERE id = :id", id=set_id)


def delete_lift_tree(tx: Tx, lift_id: int) -> None:
    for s in tx.query("SELECT id FROM workout.exercise_set WHERE lift_id = :id", id=lift_id):
        delete_set_tree(tx, s["id"])
    tx.query("DELETE FROM workout.lift WHERE id = :id", id=lift_id)


def delete_program_set_tree(tx: Tx, set_id: int) -> None:
    tx.query(
        "DELETE FROM workout.program_exercise_subset WHERE program_exercise_set_id = :id",
        id=set_id,
    )
    tx.query("DELETE FROM workout.program_exercise_set WHERE id = :id", id=set_id)


def delete_program_day_tree(tx: Tx, day_id: int) -> None:
    for s in tx.query(
        "SELECT id FROM workout.program_exercise_set WHERE program_lift_id = :id", id=day_id
    ):
        delete_program_set_tree(tx, s["id"])
    tx.query("DELETE FROM workout.program_lift WHERE id = :id", id=day_id)


def delete_program_tree(tx: Tx, program_id: int) -> None:
    for d in tx.query(
        "SELECT id FROM workout.program_lift WHERE program_id = :id", id=program_id
    ):
        delete_program_day_tree(tx, d["id"])
    tx.query("DELETE FROM workout.program WHERE id = :id", id=program_id)
