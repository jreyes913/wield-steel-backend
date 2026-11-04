"""Copying a whole program (days, sets and subsets) into an athlete's account.

Used when an athlete copies a friend's public program and when a coach sends a program to
their athletes. One transaction per copy, so DSQL's row limit per transaction applies to a
single program, which is why oversized programs are refused up front.
"""

from __future__ import annotations

from .config import PROGRAM_COPY_ROW_LIMIT
from .db import Tx
from .responses import HttpError


def _check_copy_size(tx: Tx, program_id: int) -> None:
    """Refuse a program too big to copy in one transaction."""
    [days] = tx.query(
        "SELECT COUNT(*) AS n FROM workout.program_lift WHERE program_id = :id", id=program_id
    )
    [sets] = tx.query(
        """SELECT COUNT(*) AS n FROM workout.program_exercise_set ps
             JOIN workout.program_lift pl ON pl.id = ps.program_lift_id
            WHERE pl.program_id = :id""",
        id=program_id,
    )
    [subsets] = tx.query(
        """SELECT COUNT(*) AS n FROM workout.program_exercise_subset pss
             JOIN workout.program_exercise_set ps ON ps.id = pss.program_exercise_set_id
             JOIN workout.program_lift pl ON pl.id = ps.program_lift_id
            WHERE pl.program_id = :id""",
        id=program_id,
    )
    total = days["n"] + sets["n"] + subsets["n"]
    if total > PROGRAM_COPY_ROW_LIMIT:
        raise HttpError(
            422,
            f"This program is too big to copy ({total} days, sets and exercises; "
            f"the limit is {PROGRAM_COPY_ROW_LIMIT}).",
        )


def copy_program(tx: Tx, source_id: int, target_athlete_id: int, name: str, assigned_by: int | None = None) -> dict:
    """Copy program `source_id` into `target_athlete_id`'s programs and return the new row.

    The copy is private, has no dates, and is independent of the original. `assigned_by` is
    the coach who sent it (None for a copy the owner made themselves). Call inside
    transaction(), after checking that the caller may read or send the source.
    """
    _check_copy_size(tx, source_id)
    [new_program] = tx.query(
        """INSERT INTO workout.program (athlete_id, name, start_date, end_date, is_public, assigned_by)
           VALUES (:me, :name, NULL, NULL, false, :by)
           RETURNING id, name, start_date, end_date, is_public, assigned_by""",
        me=target_athlete_id, name=name, by=assigned_by,
    )
    days = tx.query(
        "SELECT id, name FROM workout.program_lift WHERE program_id = :id ORDER BY id", id=source_id,
    )
    for day in days:
        [new_day] = tx.query(
            """INSERT INTO workout.program_lift (program_id, name)
               VALUES (:pid, :name) RETURNING id""",
            pid=new_program["id"], name=day["name"],
        )
        sets = tx.query(
            """SELECT id, position FROM workout.program_exercise_set
                WHERE program_lift_id = :id ORDER BY position, id""",
            id=day["id"],
        )
        for old_set in sets:
            [new_set] = tx.query(
                """INSERT INTO workout.program_exercise_set (program_lift_id, position)
                   VALUES (:day_id, :pos) RETURNING id""",
                day_id=new_day["id"], pos=old_set["position"],
            )
            subsets = tx.query(
                """SELECT exercise_id, position, reps FROM workout.program_exercise_subset
                    WHERE program_exercise_set_id = :id ORDER BY position, id""",
                id=old_set["id"],
            )
            for sub in subsets:
                tx.query(
                    """INSERT INTO workout.program_exercise_subset
                         (program_exercise_set_id, exercise_id, position, reps)
                       VALUES (:set_id, :ex, :pos, :reps)""",
                    set_id=new_set["id"], ex=sub["exercise_id"], pos=sub["position"], reps=sub["reps"],
                )
    return new_program
