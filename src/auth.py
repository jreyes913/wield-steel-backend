"""Who is calling, and do they own this?

API Gateway's JWT authorizer has already verified the Cognito token before
this code runs; we just read the verified claims it passes along. Anything
the caller doesn't own returns 404, so other people's IDs aren't revealed.
"""

from __future__ import annotations

import pg8000.native as pg

from .config import UNIQUE_VIOLATION
from .db import Tx, pg_code, query, runner
from .responses import HttpError

_athlete_cache: dict[str, int] = {}


def claims_from_event(event: dict) -> dict:
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims")
    )
    if not claims or not claims.get("sub"):
        # Only reachable if someone bypasses API Gateway.
        raise HttpError(401, "Not signed in")
    return claims


def current_athlete_id(claims: dict) -> int:
    """Return the caller's athlete id, creating their profile on first use."""
    sub = claims["sub"]
    if sub in _athlete_cache:
        return _athlete_cache[sub]

    rows = query("SELECT id FROM workout.athlete WHERE cognito_sub = :sub", sub=sub)
    if not rows:
        username = claims.get("username") or claims.get("cognito:username") or sub
        try:
            rows = query(
                """INSERT INTO workout.athlete (cognito_sub, username)
                   VALUES (:sub, :username) RETURNING id""",
                sub=sub,
                username=username,
            )
        except pg.DatabaseError as err:
            # Two first requests raced each other; the other one created the row.
            if pg_code(err) != UNIQUE_VIOLATION:
                raise
            rows = query("SELECT id FROM workout.athlete WHERE cognito_sub = :sub", sub=sub)

    _athlete_cache[sub] = rows[0]["id"]
    return rows[0]["id"]


def _owned(sql: str, row_id: int, me: int, label: str, db: Tx | None) -> dict:
    rows = runner(db)(sql, id=row_id, me=me)
    if not rows:
        raise HttpError(404, f"{label} not found")
    return rows[0]


def own_lift(lift_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        "SELECT * FROM workout.lift WHERE id = :id AND athlete_id = :me",
        lift_id, me, "Lift", db,
    )


def own_set(set_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        """SELECT s.* FROM workout.exercise_set s
             JOIN workout.lift l ON l.id = s.lift_id
            WHERE s.id = :id AND l.athlete_id = :me""",
        set_id, me, "Set", db,
    )


def own_subset(subset_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        """SELECT sub.* FROM workout.exercise_subset sub
             JOIN workout.exercise_set s ON s.id = sub.exercise_set_id
             JOIN workout.lift l ON l.id = s.lift_id
            WHERE sub.id = :id AND l.athlete_id = :me""",
        subset_id, me, "Subset", db,
    )


def own_program(program_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        "SELECT * FROM workout.program WHERE id = :id AND athlete_id = :me",
        program_id, me, "Program", db,
    )


def own_program_day(day_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        """SELECT pl.* FROM workout.program_lift pl
             JOIN workout.program p ON p.id = pl.program_id
            WHERE pl.id = :id AND p.athlete_id = :me""",
        day_id, me, "Program day", db,
    )


def own_program_set(set_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        """SELECT ps.* FROM workout.program_exercise_set ps
             JOIN workout.program_lift pl ON pl.id = ps.program_lift_id
             JOIN workout.program p ON p.id = pl.program_id
            WHERE ps.id = :id AND p.athlete_id = :me""",
        set_id, me, "Program set", db,
    )


def own_program_subset(subset_id: int, me: int, db: Tx | None = None) -> dict:
    return _owned(
        """SELECT pss.* FROM workout.program_exercise_subset pss
             JOIN workout.program_exercise_set ps ON ps.id = pss.program_exercise_set_id
             JOIN workout.program_lift pl ON pl.id = ps.program_lift_id
             JOIN workout.program p ON p.id = pl.program_id
            WHERE pss.id = :id AND p.athlete_id = :me""",
        subset_id, me, "Program subset", db,
    )
