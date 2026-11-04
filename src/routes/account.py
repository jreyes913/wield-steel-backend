"""Your own data: download everything (GET /me/export) and delete the account (DELETE /me)."""

from __future__ import annotations

import time

import boto3
import pg8000.native as pg
from botocore.exceptions import BotoCoreError, ClientError

from ..auth import forget_athlete
from ..cascade import delete_lift_tree, delete_program_tree
from ..config import (
    AWS_REGION, COGNITO_USER_POOL_ID, DELETE_FRIENDSHIP_BATCH, DELETE_TIME_BUDGET_SECONDS,
    UNDEFINED_TABLE,
)
from ..db import Tx, pg_code, query, transaction
from ..responses import HttpError, respond
from ..validation import to_text
from .friends import _delete_row, _name_key, _others
from .profile import PROFILE_COLUMNS

_cognito = boto3.client("cognito-idp", region_name=AWS_REGION)


# ---------- Export ----------

def _export_lifts(me: int) -> list[dict]:
    """All the caller's lifts with sets and subsets nested, using one query."""
    rows = query(
        """SELECT l.id AS lift_id, l.date, l.body_weight,
                  s.id AS set_id, s.position AS set_position,
                  sub.id AS subset_id, sub.position AS subset_position,
                  sub.reps, sub.rir, sub.weight, e.name AS exercise_name
             FROM workout.lift l
             LEFT JOIN workout.exercise_set s ON s.lift_id = l.id
             LEFT JOIN workout.exercise_subset sub ON sub.exercise_set_id = s.id
             LEFT JOIN workout.exercise e ON e.id = sub.exercise_id
            WHERE l.athlete_id = :me
            ORDER BY l.date DESC, l.id DESC, s.position, s.id, sub.position, sub.id""",
        me=me,
    )
    lifts: dict[int, dict] = {}
    sets: dict[int, dict] = {}
    for r in rows:
        lift = lifts.setdefault(r["lift_id"], {
            "id": r["lift_id"], "date": r["date"], "body_weight": r["body_weight"], "sets": [],
        })
        if r["set_id"] is None:
            continue
        if r["set_id"] not in sets:
            sets[r["set_id"]] = {"id": r["set_id"], "position": r["set_position"], "subsets": []}
            lift["sets"].append(sets[r["set_id"]])
        if r["subset_id"] is not None:
            sets[r["set_id"]]["subsets"].append({
                "id": r["subset_id"], "position": r["subset_position"],
                "exercise": r["exercise_name"],
                "reps": r["reps"], "rir": r["rir"], "weight": r["weight"],
            })
    return list(lifts.values())


def _export_programs(me: int) -> list[dict]:
    """All the caller's programs with days, sets and subsets nested."""
    rows = query(
        """SELECT p.id AS program_id, p.name AS program_name, p.start_date, p.end_date,
                  pl.id AS day_id, pl.name AS day_name,
                  ps.id AS set_id, ps.position AS set_position,
                  pss.id AS subset_id, pss.position AS subset_position, pss.reps,
                  e.name AS exercise_name
             FROM workout.program p
             LEFT JOIN workout.program_lift pl ON pl.program_id = p.id
             LEFT JOIN workout.program_exercise_set ps ON ps.program_lift_id = pl.id
             LEFT JOIN workout.program_exercise_subset pss ON pss.program_exercise_set_id = ps.id
             LEFT JOIN workout.exercise e ON e.id = pss.exercise_id
            WHERE p.athlete_id = :me
            ORDER BY p.id DESC, pl.id, ps.position, ps.id, pss.position, pss.id""",
        me=me,
    )
    programs: dict[int, dict] = {}
    days: dict[int, dict] = {}
    sets: dict[int, dict] = {}
    for r in rows:
        program = programs.setdefault(r["program_id"], {
            "id": r["program_id"], "name": r["program_name"],
            "start_date": r["start_date"], "end_date": r["end_date"], "days": [],
        })
        if r["day_id"] is None:
            continue
        if r["day_id"] not in days:
            days[r["day_id"]] = {"id": r["day_id"], "name": r["day_name"], "sets": []}
            program["days"].append(days[r["day_id"]])
        if r["set_id"] is None:
            continue
        if r["set_id"] not in sets:
            sets[r["set_id"]] = {"id": r["set_id"], "position": r["set_position"], "subsets": []}
            days[r["day_id"]]["sets"].append(sets[r["set_id"]])
        if r["subset_id"] is not None:
            sets[r["set_id"]]["subsets"].append({
                "id": r["subset_id"], "position": r["subset_position"],
                "exercise": r["exercise_name"], "reps": r["reps"],
            })
    return list(programs.values())


def _public_list(me: int, statuses: str, i_am: str) -> list[dict]:
    """Public fields (username and name) of athletes on the other end of my rows."""
    rows = _others(me, i_am, statuses)
    rows.sort(key=_name_key)
    return [{k: r[k] for k in ("username", "first_name", "last_name")} for r in rows]


def export_me(req):
    """Everything the caller owns, as JSON. Never includes cognito_sub or internal ids of others."""
    me = req["athlete_id"]
    [profile] = query(f"SELECT {PROFILE_COLUMNS} FROM workout.athlete WHERE id = :me", me=me)
    friends = (_public_list(me, "('accepted')", "requester")
               + _public_list(me, "('accepted')", "addressee"))
    friends.sort(key=lambda a: a["username"])
    return respond(200, {
        "profile": {k: v for k, v in profile.items() if k != "id"},
        "lifts": _export_lifts(me),
        "programs": _export_programs(me),
        "friends": friends,
        "blocked": _public_list(me, "('blocked')", "requester"),  # only blocks the caller made
    })


# ---------- Delete ----------

def _delete_in_batches(me: int, deadline: float) -> bool:
    """Delete the caller's data, one small transaction at a time. True when nothing is left.

    Safe to repeat: every step starts from what is still in the database, and the athlete
    row goes last, so a stopped run can simply be started again.
    """
    for table, delete_tree in (("lift", delete_lift_tree), ("program", delete_program_tree)):
        for row in query(f"SELECT id FROM workout.{table} WHERE athlete_id = :me", me=me):
            if time.monotonic() > deadline:
                return False
            transaction(lambda tx, row_id=row["id"]: delete_tree(tx, row_id))

    pairs = query(
        """SELECT requester_id, addressee_id FROM workout.friendship
            WHERE requester_id = :me OR addressee_id = :me""",
        me=me,
    )
    for i in range(0, len(pairs), DELETE_FRIENDSHIP_BATCH):
        if time.monotonic() > deadline:
            return False
        batch = pairs[i:i + DELETE_FRIENDSHIP_BATCH]

        def work(tx: Tx, batch=batch):
            for p in batch:
                _delete_row(tx, p["requester_id"], p["addressee_id"])

        transaction(work)

    if time.monotonic() > deadline:
        return False
    try:
        # A person's attempt log (at most a few dozen rows, since old ones are purged on read).
        query("DELETE FROM workout.rate_limit WHERE athlete_id = :me", me=me)
    except pg.DatabaseError as err:
        if pg_code(err) != UNDEFINED_TABLE:  # table not created yet: nothing to delete
            raise
    query("DELETE FROM workout.athlete WHERE id = :me", me=me)
    return True


def _delete_cognito_user(claims: dict) -> None:
    """Remove the sign-in. Already gone counts as done, so a repeat call is harmless."""
    try:
        _cognito.admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=claims.get("username") or claims.get("cognito:username") or claims["sub"],
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "UserNotFoundException":
            return
        raise


def delete_me(req):
    """Delete the caller's data and sign-in. Answers 202 when it needs calling again to finish."""
    me = req["athlete_id"]
    claims = req["claims"]
    confirm = to_text(req["body"].get("confirm"), "confirm").lower()
    [row] = query("SELECT username FROM workout.athlete WHERE id = :me", me=me)
    if confirm != row["username"].lower():
        raise HttpError(400, "Type your username exactly to confirm")

    deadline = time.monotonic() + DELETE_TIME_BUDGET_SECONDS
    if not _delete_in_batches(me, deadline):
        return respond(202, {"done": False, "message": "Still deleting; send the same request again"})

    forget_athlete(claims["sub"])
    try:
        _delete_cognito_user(claims)
    except (ClientError, BotoCoreError):
        raise HttpError(
            502,
            "Your data has been deleted, but we couldn't remove your sign-in. "
            "Please contact support to finish closing your account.",
        )
    return respond(200, {"done": True})
