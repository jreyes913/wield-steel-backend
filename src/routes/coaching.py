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

from ..auth import own_program, require_coach_of
from ..db import Tx, query, transaction
from ..program_copy import detach_link
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
        "groups": _groups(me),
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
    detach_link(coach_id, athlete_id)  # the athlete keeps what was sent, no longer kept in sync

    def work(tx: Tx):
        rows = tx.query(
            "SELECT 1 AS ok FROM workout.coaching WHERE coach_id = :coach AND athlete_id = :athlete AND status = 'accepted'",
            coach=coach_id, athlete=athlete_id,
        )
        if not rows:
            raise HttpError(404, f"{label} not found")
        tx.query("DELETE FROM workout.coaching WHERE coach_id = :coach AND athlete_id = :athlete",
                 coach=coach_id, athlete=athlete_id)
        tx.query("DELETE FROM workout.coach_group_member WHERE coach_id = :coach AND athlete_id = :athlete",
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
            WHERE athlete_id = :athlete AND is_public = true ORDER BY id DESC""",
        athlete=athlete_id,
    )
    return respond(200, rows)


def get_athlete_program(req):
    athlete_id = path_id(req)
    require_coach_of(athlete_id, req["athlete_id"])
    rows = query(
        """SELECT id, name, start_date, end_date FROM workout.program
            WHERE id = :id AND athlete_id = :athlete AND is_public = true""",
        id=int(req["params"]["program_id"]), athlete=athlete_id,
    )
    if not rows:
        raise HttpError(404, "Program not found")
    return respond(200, _program_detail(rows[0]))


# ---------- Sending a program ----------

MAX_SEND_ATHLETES = 50


def send_program(req):
    """Give each chosen athlete a program that follows one of mine (id = my program).

    Body: {"athlete_ids": [...]}. Only athletes I coach (accepted) can receive it; the athlete
    needs no action. The athlete gets a row that shows my program live: when I add or delete a
    day, set or exercise it changes for them too. It is read-only and permanently private for
    them. Sending the same program again is harmless. The program itself can stay private:
    sending is not sharing. Each athlete is its own transaction and the answer lists the outcome
    per athlete.
    """
    me = req["athlete_id"]
    program_id = path_id(req)
    require_fields(req["body"], "athlete_ids")
    raw = req["body"]["athlete_ids"]
    if not isinstance(raw, list) or not raw or not all(isinstance(i, int) and not isinstance(i, bool) for i in raw):
        raise HttpError(400, "athlete_ids must be a list of athlete ids")
    athlete_ids = list(dict.fromkeys(raw))  # drop repeats, keep the order
    if len(athlete_ids) > MAX_SEND_ATHLETES:
        raise HttpError(400, f"At most {MAX_SEND_ATHLETES} athletes at a time")

    source = own_program(program_id, me)
    if source["assigned_by"] is not None:
        raise HttpError(400, "A program a coach sent you can't be sent on")
    results = []
    for athlete_id in athlete_ids:
        def work(tx: Tx, athlete_id=athlete_id):
            require_coach_of(athlete_id, me, tx)  # 404 unless I coach them
            have = tx.query(
                "SELECT id FROM workout.program WHERE athlete_id = :a AND source_program_id = :s",
                a=athlete_id, s=program_id,
            )
            if have:
                return have[0]["id"], True
            [row] = tx.query(
                """INSERT INTO workout.program
                     (athlete_id, name, start_date, end_date, is_public, assigned_by, source_program_id)
                   VALUES (:a, :name, NULL, NULL, false, :by, :s) RETURNING id""",
                a=athlete_id, name=source["name"], by=me, s=program_id,
            )
            return row["id"], False

        try:
            new_id, already = transaction(work)
            results.append({"athlete_id": athlete_id, "ok": True, "program_id": new_id, "already_sent": already})
        except HttpError as err:
            results.append({"athlete_id": athlete_id, "ok": False,
                            "error": "Not one of your athletes" if err.status == 404 else str(err)})
    return respond(200, {"results": results})


# ---------- Groups ----------
# A coach's private way to sort their athletes (for example "Powerlifting" or "Beginners") so a
# program can be sent to a whole group. The athletes never see groups. An athlete can be in
# several groups, and only athletes I coach (accepted) can be in one.

MAX_GROUPS = 20
MAX_GROUP_NAME = 40


def _groups(me: int) -> list[dict]:
    """My groups as {id, name, athlete_ids}, by name."""
    groups = query("SELECT id, name FROM workout.coach_group WHERE coach_id = :me", me=me)
    members: dict[int, list[int]] = {}
    for r in query("SELECT group_id, athlete_id FROM workout.coach_group_member WHERE coach_id = :me", me=me):
        members.setdefault(r["group_id"], []).append(r["athlete_id"])
    groups.sort(key=lambda g: g["name"].lower())
    return [{"id": g["id"], "name": g["name"], "athlete_ids": sorted(members.get(g["id"], []))} for g in groups]


def _group_name(body: dict) -> str:
    name = to_text(body["name"], "name")
    if len(name) > MAX_GROUP_NAME:
        raise HttpError(400, f"A group name can be at most {MAX_GROUP_NAME} characters")
    return name


def _member_ids(tx: Tx, me: int, raw) -> list[int]:
    """Check a list of athlete ids: each must be an athlete I coach. Returns it without repeats."""
    if not isinstance(raw, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in raw):
        raise HttpError(400, "athlete_ids must be a list of athlete ids")
    ids = list(dict.fromkeys(raw))
    mine = {r["athlete_id"] for r in tx.query(
        "SELECT athlete_id FROM workout.coaching WHERE coach_id = :me AND status = 'accepted'", me=me)}
    if any(i not in mine for i in ids):
        raise HttpError(400, "Groups can only hold athletes you coach")
    return ids


def _name_taken(tx: Tx, me: int, name: str, except_id: int | None = None) -> bool:
    rows = tx.query(
        "SELECT id FROM workout.coach_group WHERE coach_id = :me AND lower(name) = lower(:name)",
        me=me, name=name,
    )
    return any(r["id"] != except_id for r in rows)


def _set_members(tx: Tx, me: int, group_id: int, ids: list[int]) -> None:
    tx.query("DELETE FROM workout.coach_group_member WHERE group_id = :g", g=group_id)
    for athlete_id in ids:
        tx.query(
            "INSERT INTO workout.coach_group_member (group_id, coach_id, athlete_id) VALUES (:g, :me, :a)",
            g=group_id, me=me, a=athlete_id,
        )


def create_group(req):
    """New group; the body may also list its athlete_ids."""
    me = req["athlete_id"]
    body = req["body"]
    require_fields(body, "name")
    name = _group_name(body)

    def work(tx: Tx):
        ids = _member_ids(tx, me, body.get("athlete_ids", []))
        if tx.query("SELECT COUNT(*) AS n FROM workout.coach_group WHERE coach_id = :me", me=me)[0]["n"] >= MAX_GROUPS:
            raise HttpError(422, f"You can have at most {MAX_GROUPS} groups")
        if _name_taken(tx, me, name):
            raise HttpError(409, "You already have a group with that name")
        [row] = tx.query(
            "INSERT INTO workout.coach_group (coach_id, name) VALUES (:me, :name) RETURNING id",
            me=me, name=name,
        )
        _set_members(tx, me, row["id"], ids)
        return {"id": row["id"], "name": name, "athlete_ids": sorted(ids)}

    return respond(201, transaction(work))


def update_group(req):
    """Rename a group and/or replace its athletes (send only what changed)."""
    me = req["athlete_id"]
    group_id = path_id(req)
    body = req["body"]
    if "name" not in body and "athlete_ids" not in body:
        raise HttpError(400, "Send at least one of: name, athlete_ids")

    def work(tx: Tx):
        rows = tx.query("SELECT id, name FROM workout.coach_group WHERE id = :g AND coach_id = :me", g=group_id, me=me)
        if not rows:
            raise HttpError(404, "Group not found")
        name = rows[0]["name"]
        if "name" in body:
            name = _group_name(body)
            if _name_taken(tx, me, name, except_id=group_id):
                raise HttpError(409, "You already have a group with that name")
            tx.query("UPDATE workout.coach_group SET name = :name WHERE id = :g", name=name, g=group_id)
        if "athlete_ids" in body:
            _set_members(tx, me, group_id, _member_ids(tx, me, body["athlete_ids"]))
        return {"id": group_id, "name": name,
                "athlete_ids": sorted(r["athlete_id"] for r in tx.query(
                    "SELECT athlete_id FROM workout.coach_group_member WHERE group_id = :g", g=group_id))}

    return respond(200, transaction(work))


def delete_group(req):
    """Delete a group. The athletes stay; only the grouping goes."""
    me = req["athlete_id"]
    group_id = path_id(req)

    def work(tx: Tx):
        if not tx.query("SELECT 1 AS ok FROM workout.coach_group WHERE id = :g AND coach_id = :me", g=group_id, me=me):
            raise HttpError(404, "Group not found")
        tx.query("DELETE FROM workout.coach_group_member WHERE group_id = :g", g=group_id)
        tx.query("DELETE FROM workout.coach_group WHERE id = :g", g=group_id)

    transaction(work)
    return respond(200, {"deleted": group_id})
