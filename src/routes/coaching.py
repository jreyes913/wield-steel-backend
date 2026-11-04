"""Coaching: an athlete lets another athlete see their training, read-only.

Anyone can be a coach and an athlete at once; "coach" is a link, not an account type. One row
in workout.coaching per pair (coach_id asked to coach athlete_id). Only the athlete can accept,
change what is shared, or end it, and the coach can end it too. A coach sees the athlete's
username and name, programs and logged lifts, and body weight only when the athlete allowed
it (can_see_body_weight). Never DOB, gender, height or cognito_sub. Every query that returns
an athlete's data to a coach lists its columns explicitly.

status is 'pending', 'accepted' or 'declined'. A declined request looks pending to the coach,
so it can't be used to probe. An athlete who has blocked a coach (friendship status
'blocked') is shown as "no such username", and blocking ends any coaching link.
"""

from __future__ import annotations

from ..auth import require_coach_of
from ..db import Tx, query, transaction
from ..responses import HttpError, respond
from ..validation import path_id, require_fields, to_text
from .friends import _as_athlete_entry, _created, _name_key, _public_profile
from .lifts import _lift_detail
from .profile import PUBLIC_PROFILE_COLUMNS
from .programs import _program_detail

ANSWERS = ("accepted", "declined")


def _flag(body: dict, key: str) -> bool:
    value = body[key]
    if not isinstance(value, bool):
        raise HttpError(400, f"{key} must be true or false")
    return value


def _links(me: int, i_am: str, statuses: str) -> list[dict]:
    """The athletes on the other end of my coaching rows.

    i_am is "coach" or "athlete"; statuses is a fixed SQL list from this module.
    """
    theirs = "athlete_id" if i_am == "coach" else "coach_id"
    return query(
        f"""SELECT a.id, a.username, a.first_name, a.last_name,
                   c.created_at, c.updated_at, c.can_see_body_weight
              FROM workout.coaching c
              JOIN workout.athlete a ON a.id = c.{theirs}
             WHERE c.{i_am}_id = :me AND c.status IN {statuses}""",
        me=me,
    )


def _entry(row: dict, date_key: str, date) -> dict:
    return {**_as_athlete_entry(row, date_key, date), "can_see_body_weight": bool(row["can_see_body_weight"])}


# ---------- Lists ----------

def list_coaching(req):
    """My coaches, the athletes I coach, and pending requests in both directions."""
    me = req["athlete_id"]
    coaches = sorted(_links(me, "athlete", "('accepted')"), key=_name_key)
    coached = sorted(_links(me, "coach", "('accepted')"), key=_name_key)
    incoming = sorted(_links(me, "athlete", "('pending')"), key=_created, reverse=True)
    outgoing = sorted(_links(me, "coach", "('pending', 'declined')"), key=_created, reverse=True)
    return respond(200, {
        "coaches": [_entry(r, "since", r["updated_at"]) for r in coaches],
        "athletes": [_entry(r, "since", r["updated_at"]) for r in coached],
        "incoming": [_entry(r, "sent_at", _created(r)) for r in incoming],
        "outgoing": [_entry(r, "sent_at", _created(r)) for r in outgoing],
    })


# ---------- Requests ----------

def send_request(req):
    """Ask to coach someone, by username."""
    me = req["athlete_id"]
    body = req["body"]
    require_fields(body, "username")
    username = to_text(body["username"], "username")

    def work(tx: Tx):
        found = tx.query(
            f"SELECT {PUBLIC_PROFILE_COLUMNS} FROM workout.athlete WHERE lower(username) = lower(:username)",
            username=username,
        )
        if not found:
            raise HttpError(404, "No athlete with that username")
        other = found[0]
        if other["id"] == me:
            raise HttpError(400, "You can't coach yourself")
        blocked = tx.query(
            """SELECT 1 AS ok FROM workout.friendship
                WHERE status = 'blocked' AND requester_id = :other AND addressee_id = :me""",
            other=other["id"], me=me,
        )
        if blocked:
            raise HttpError(404, "No athlete with that username")
        rows = tx.query(
            "SELECT status FROM workout.coaching WHERE coach_id = :me AND athlete_id = :other",
            me=me, other=other["id"],
        )
        if rows:
            raise HttpError(409, "You already coach them" if rows[0]["status"] == "accepted"
                            else "Request already sent")
        tx.query(
            """INSERT INTO workout.coaching
                 (coach_id, athlete_id, status, can_see_body_weight, created_at, updated_at)
               VALUES (:me, :other, 'pending', false, now(), now())""",
            me=me, other=other["id"],
        )
        return {"status": "pending", "athlete": other}

    return respond(201, transaction(work))


def answer_request(req):
    """The athlete accepts or declines a coach's request (the id in the path is the coach's)."""
    coach_id = path_id(req)
    me = req["athlete_id"]
    body = req["body"]
    require_fields(body, "status")
    status = body["status"]
    if status not in ANSWERS:
        raise HttpError(400, "status must be 'accepted' or 'declined'")
    share_weight = _flag(body, "can_see_body_weight") if "can_see_body_weight" in body else False

    def work(tx: Tx):
        rows = tx.query(
            "SELECT status FROM workout.coaching WHERE coach_id = :coach AND athlete_id = :me",
            coach=coach_id, me=me,
        )
        if not rows:
            raise HttpError(404, "Request not found")
        if rows[0]["status"] != "pending":
            raise HttpError(409, "Request already answered")
        tx.query(
            """UPDATE workout.coaching
                  SET status = :status, can_see_body_weight = :weight, updated_at = now()
                WHERE coach_id = :coach AND athlete_id = :me AND status = 'pending'""",
            status=status, weight=share_weight and status == "accepted", coach=coach_id, me=me,
        )
        return {"status": status, "athlete": _public_profile(coach_id, tx)}

    return respond(200, transaction(work))


def cancel_request(req):
    """A coach withdraws a request that has not been accepted (id = the athlete's)."""
    athlete_id = path_id(req)
    me = req["athlete_id"]

    def work(tx: Tx):
        rows = tx.query(
            """SELECT status FROM workout.coaching
                WHERE coach_id = :me AND athlete_id = :athlete AND status IN ('pending', 'declined')""",
            me=me, athlete=athlete_id,
        )
        if not rows:
            raise HttpError(404, "Request not found")
        tx.query("DELETE FROM workout.coaching WHERE coach_id = :me AND athlete_id = :athlete",
                 me=me, athlete=athlete_id)

    transaction(work)
    return respond(200, {"deleted": athlete_id})


# ---------- Managing a link ----------

def set_sharing(req):
    """The athlete changes what one coach may see (id = the coach's)."""
    coach_id = path_id(req)
    me = req["athlete_id"]
    require_fields(req["body"], "can_see_body_weight")
    weight = _flag(req["body"], "can_see_body_weight")

    def work(tx: Tx):
        rows = tx.query(
            "SELECT 1 AS ok FROM workout.coaching WHERE coach_id = :coach AND athlete_id = :me AND status = 'accepted'",
            coach=coach_id, me=me,
        )
        if not rows:
            raise HttpError(404, "Coach not found")
        # updated_at stays put: it is when coaching began.
        tx.query(
            "UPDATE workout.coaching SET can_see_body_weight = :w WHERE coach_id = :coach AND athlete_id = :me",
            w=weight, coach=coach_id, me=me,
        )

    transaction(work)
    return respond(200, {"coach_id": coach_id, "can_see_body_weight": weight})


def _end(coach_id: int, athlete_id: int, label: str) -> None:
    def work(tx: Tx):
        rows = tx.query(
            "SELECT 1 AS ok FROM workout.coaching WHERE coach_id = :coach AND athlete_id = :athlete AND status = 'accepted'",
            coach=coach_id, athlete=athlete_id,
        )
        if not rows:
            raise HttpError(404, f"{label} not found")
        tx.query("DELETE FROM workout.coaching WHERE coach_id = :coach AND athlete_id = :athlete",
                 coach=coach_id, athlete=athlete_id)

    transaction(work)


def remove_coach(req):
    """The athlete ends coaching (id = the coach's)."""
    coach_id = path_id(req)
    _end(coach_id, req["athlete_id"], "Coach")
    return respond(200, {"deleted": coach_id})


def stop_coaching(req):
    """The coach ends coaching (id = the athlete's)."""
    athlete_id = path_id(req)
    _end(req["athlete_id"], athlete_id, "Athlete")
    return respond(200, {"deleted": athlete_id})


# ---------- What a coach can read ----------

def get_athlete(req):
    athlete_id = path_id(req)
    link = require_coach_of(athlete_id, req["athlete_id"])
    return respond(200, {**_public_profile(athlete_id), "can_see_body_weight": link["can_see_body_weight"]})


def list_athlete_lifts(req):
    athlete_id = path_id(req)
    link = require_coach_of(athlete_id, req["athlete_id"])
    columns = "id, date, body_weight" if link["can_see_body_weight"] else "id, date"
    rows = query(
        f"SELECT {columns} FROM workout.lift WHERE athlete_id = :athlete ORDER BY date DESC, id DESC",
        athlete=athlete_id,
    )
    return respond(200, rows)


def get_athlete_lift(req):
    athlete_id = path_id(req)
    link = require_coach_of(athlete_id, req["athlete_id"])
    columns = "id, date, athlete_id, body_weight" if link["can_see_body_weight"] else "id, date, athlete_id"
    rows = query(
        f"SELECT {columns} FROM workout.lift WHERE id = :id AND athlete_id = :athlete",
        id=int(req["params"]["lift_id"]), athlete=athlete_id,
    )
    if not rows:
        raise HttpError(404, "Lift not found")
    return respond(200, _lift_detail(rows[0]))


def list_athlete_programs(req):
    athlete_id = path_id(req)
    require_coach_of(athlete_id, req["athlete_id"])
    rows = query(
        """SELECT id, name, start_date, end_date FROM workout.program
            WHERE athlete_id = :athlete ORDER BY id DESC""",
        athlete=athlete_id,
    )
    return respond(200, rows)


def get_athlete_program(req):
    athlete_id = path_id(req)
    require_coach_of(athlete_id, req["athlete_id"])
    rows = query(
        """SELECT id, name, start_date, end_date FROM workout.program
            WHERE id = :id AND athlete_id = :athlete""",
        id=int(req["params"]["program_id"]), athlete=athlete_id,
    )
    if not rows:
        raise HttpError(404, "Program not found")
    return respond(200, _program_detail(rows[0]))
