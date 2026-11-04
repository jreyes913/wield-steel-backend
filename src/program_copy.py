"""Copying a whole program (days, sets and subsets) into an athlete's account.

Used when an athlete copies a friend's public program (a clean copy, no syncing). Also holds
what happens when a coach's program stops being kept in sync with an athlete's. One transaction
per copy, so DSQL's row limit per transaction applies to a single program, which is why
oversized programs are refused up front.
"""

from __future__ import annotations

from .config import PROGRAM_COPY_ROW_LIMIT
from .cascade import delete_program_day_tree
from .db import Tx, query, transaction
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
    for day in tx.query(
        "SELECT id, name FROM workout.program_lift WHERE program_id = :id ORDER BY id", id=source_id,
    ):
        _copy_day(tx, day, new_program["id"])
    return new_program


def _copy_day(tx: Tx, day: dict, program_id: int) -> None:
    """Copy one day (with its sets and subsets) of another program into `program_id`."""
    [new_day] = tx.query(
        """INSERT INTO workout.program_lift (program_id, name)
           VALUES (:pid, :name) RETURNING id""",
        pid=program_id, name=day["name"],
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


# ---------- Programs a coach keeps in sync ----------
# A program sent by a coach is a row with source_program_id pointing at the coach's program and
# no days of its own: readers use the coach's days, so every change shows at once. When the
# sync ends (the coach deletes the program, or coaching stops) the row is filled with a copy of
# what it showed, so the athlete keeps it. It stays private and cannot be made public.

def detach_program(program_id: int) -> None:
    """Turn one synced program into an ordinary private copy of what it currently shows.

    Day by day, each in its own transaction (so a large program stays under DSQL's row limit),
    and safe to run again after a stop halfway: leftover days are cleared first and the link
    to the coach's program is removed last.
    """
    rows = query(
        "SELECT id, source_program_id FROM workout.program WHERE id = :id", id=program_id
    )
    if not rows or rows[0]["source_program_id"] is None:
        return
    source_id = rows[0]["source_program_id"]
    for day in query("SELECT id FROM workout.program_lift WHERE program_id = :id", id=program_id):
        transaction(lambda tx, d=day["id"]: delete_program_day_tree(tx, d))
    for day in query(
        "SELECT id, name FROM workout.program_lift WHERE program_id = :id ORDER BY id", id=source_id
    ):
        transaction(lambda tx, d=day: _copy_day(tx, d, program_id))

    def finish(tx: Tx):
        source = tx.query(
            "SELECT name, start_date, end_date FROM workout.program WHERE id = :id", id=source_id
        )
        if source:
            tx.query(
                """UPDATE workout.program SET name = :name, start_date = :start, end_date = :end
                    WHERE id = :id""",
                name=source[0]["name"], start=source[0]["start_date"], end=source[0]["end_date"],
                id=program_id,
            )
        tx.query("UPDATE workout.program SET source_program_id = NULL WHERE id = :id", id=program_id)

    transaction(finish)


def detach_copies_of(source_id: int) -> None:
    """End the sync for every athlete's copy of one of the coach's programs."""
    for row in query("SELECT id FROM workout.program WHERE source_program_id = :id", id=source_id):
        detach_program(row["id"])


def detach_link(coach_id: int, athlete_id: int) -> None:
    """End the sync for everything this coach sent this athlete (before the link is removed)."""
    for row in query(
        """SELECT id FROM workout.program
            WHERE athlete_id = :athlete AND assigned_by = :coach AND source_program_id IS NOT NULL""",
        athlete=athlete_id, coach=coach_id,
    ):
        detach_program(row["id"])
