"""Who is calling, and do they own this?

API Gateway's JWT authorizer has already verified the Cognito token before
this code runs; we just read the verified claims it passes along. Anything
the caller doesn't own returns 404, so other people's IDs aren't revealed.
The one exception is a friend's data: require_friend() and the friend_* helpers
below let an accepted friend read (never change) a friend's lifts and programs.
"""

from __future__ import annotations

import time

import boto3
import pg8000.native as pg
from botocore.exceptions import ClientError

from .config import AWS_REGION, COGNITO_USER_POOL_ID, UNIQUE_VIOLATION
from .db import Tx, pg_code, query, runner
from .responses import HttpError

_cognito = boto3.client("cognito-idp", region_name=AWS_REGION)

# sub -> (athlete id, time to look it up again). Short, so a container that still holds the
# id of a deleted account stops writing for it within a minute.
_athlete_cache: dict[str, tuple[int, float]] = {}
ATHLETE_CACHE_SECONDS = 60


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


def _require_cognito_user(username: str) -> None:
    """401 unless the Cognito user still exists.

    A deleted user's access token stays valid for up to an hour, and must not bring back
    an empty profile. Checked only when a profile is about to be created.
    """
    if not COGNITO_USER_POOL_ID:
        return
    try:
        _cognito.admin_get_user(UserPoolId=COGNITO_USER_POOL_ID, Username=username)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "UserNotFoundException":
            raise HttpError(401, "This account no longer exists") from None
        raise


def current_athlete_id(claims: dict) -> int:
    """Return the caller's athlete id, creating their profile on first use."""
    sub = claims["sub"]
    hit = _athlete_cache.get(sub)
    if hit and hit[1] > time.monotonic():
        return hit[0]

    rows = query("SELECT id FROM workout.athlete WHERE cognito_sub = :sub", sub=sub)
    if not rows:
        username = claims.get("username") or claims.get("cognito:username") or sub
        _require_cognito_user(username)
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

    _athlete_cache[sub] = (rows[0]["id"], time.monotonic() + ATHLETE_CACHE_SECONDS)
    return rows[0]["id"]


def forget_athlete(sub: str) -> None:
    """Drop the cached athlete id for a Cognito user (used when an account is deleted)."""
    _athlete_cache.pop(sub, None)


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


def require_friend(other_id: int, me: int, db: Tx | None = None) -> None:
    """404 unless an accepted friendship exists between the two athletes, in either direction.

    Pending and declined requests don't count, and neither does asking about yourself.
    """
    rows = runner(db)(
        """SELECT 1 AS ok FROM workout.friendship
            WHERE status = 'accepted'
              AND ((requester_id = :me AND addressee_id = :other)
                OR (requester_id = :other AND addressee_id = :me))""",
        me=me, other=other_id,
    )
    if not rows:
        raise HttpError(404, "Athlete not found")


def lifts_visible(friend_id: int, me: int, db: Tx | None = None) -> bool:
    """False when the friend has hidden their lifts from me (or we aren't friends).

    Callers answer exactly as they would for a friend with no lifts, so the setting
    is never revealed: an empty list, or 404 for a single lift.
    """
    rows = runner(db)(
        """SELECT requester_id, requester_hides_lifts, addressee_hides_lifts
             FROM workout.friendship
            WHERE status = 'accepted'
              AND ((requester_id = :me AND addressee_id = :other)
                OR (requester_id = :other AND addressee_id = :me))""",
        me=me, other=friend_id,
    )
    if not rows:
        return False
    row = rows[0]
    hidden = row["requester_hides_lifts"] if row["requester_id"] == friend_id else row["addressee_hides_lifts"]
    return not hidden


def friend_lift(lift_id: int, friend_id: int, db: Tx | None = None) -> dict:
    """A friend's lift, with only the columns a friend may see (never body_weight).

    Call require_friend() first; this only checks that the lift is theirs.
    """
    return _owned(
        "SELECT id, date, athlete_id FROM workout.lift WHERE id = :id AND athlete_id = :me",
        lift_id, friend_id, "Lift", db,
    )


def friend_program(program_id: int, friend_id: int, db: Tx | None = None) -> dict:
    """A friend's program row. Call require_friend() first; this checks it is theirs and public."""
    return _owned(
        """SELECT id, name, start_date, end_date FROM workout.program
            WHERE id = :id AND athlete_id = :me AND is_public = true""",
        program_id, friend_id, "Program", db,
    )


def require_coach_of(athlete_id: int, me: int, db: Tx | None = None) -> dict:
    """The accepted coaching link where I coach `athlete_id`; 404 if there isn't one.

    Returns {"can_see_body_weight": bool}. Pending and declined links, and asking about
    yourself, give the same 404 as an athlete who doesn't exist.
    """
    rows = runner(db)(
        """SELECT can_see_body_weight FROM workout.coaching
            WHERE coach_id = :me AND athlete_id = :athlete AND status = 'accepted'""",
        me=me, athlete=athlete_id,
    )
    if not rows:
        raise HttpError(404, "Athlete not found")
    return {"can_see_body_weight": bool(rows[0]["can_see_body_weight"])}
