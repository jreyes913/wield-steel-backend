"""Friends: requests, the friend list, blocking, and read-only access to a friend's data.

A friendship is one row in workout.friendship per pair of athletes (requester_id
sent the request to addressee_id). Two athletes are friends when either direction
has status 'accepted'. A friend sees another athlete's username and name, their
programs and their logged lifts, and nothing else: never body weight, DOB, gender
or height. Every query that returns a friend's data lists its columns explicitly.

Other columns: created_at is when a request was sent (updated_at is the last status
change, so it is when a friendship began). requester_hides_lifts / addressee_hides_lifts
hide that side's lifts from the other. status 'blocked' means requester_id blocked
addressee_id; blocked rows never show up in the friend or request lists.
"""

from __future__ import annotations

from ..auth import friend_lift, friend_program, lifts_visible, require_friend
from ..db import Tx, query, runner, transaction
from ..program_copy import copy_program, detach_link
from ..responses import HttpError, respond
from ..validation import path_id, require_fields, to_text
from .lifts import _lift_detail
from .profile import PUBLIC_PROFILE_COLUMNS
from .programs import _program_detail

ANSWERS = ("accepted", "declined")


# ---------- Helpers ----------

def _public_profile(athlete_id: int, db: Tx | None = None) -> dict:
    rows = runner(db)(
        f"SELECT {PUBLIC_PROFILE_COLUMNS} FROM workout.athlete WHERE id = :id",
        id=athlete_id,
    )
    if not rows:
        raise HttpError(404, "Athlete not found")
    return rows[0]


def _pair_rows(tx: Tx, me: int, other: int) -> list[dict]:
    """The friendship row(s) between two athletes, in either direction."""
    return tx.query(
        """SELECT requester_id, addressee_id, status FROM workout.friendship
            WHERE (requester_id = :me AND addressee_id = :other)
               OR (requester_id = :other AND addressee_id = :me)""",
        me=me, other=other,
    )


def _delete_row(tx: Tx, requester_id: int, addressee_id: int) -> None:
    tx.query(
        "DELETE FROM workout.friendship WHERE requester_id = :req AND addressee_id = :addr",
        req=requester_id, addr=addressee_id,
    )


def _accept(tx: Tx, requester_id: int, addressee_id: int) -> None:
    tx.query(
        """UPDATE workout.friendship SET status = 'accepted', updated_at = now()
            WHERE requester_id = :req AND addressee_id = :addr AND status = 'pending'""",
        req=requester_id, addr=addressee_id,
    )


def _created(row: dict):
    """When a row was created. Older rows have no created_at, so fall back to updated_at."""
    return row["created_at"] or row["updated_at"]


def _name_key(athlete: dict) -> tuple:
    """Sort by name, then username. Athletes with no name come after the named ones."""
    name = f"{athlete['first_name'] or ''} {athlete['last_name'] or ''}".strip().lower()
    return (name == "", name, athlete["username"].lower())


def _others(me: int, i_am: str, statuses: str) -> list[dict]:
    """The athletes on the other end of my friendship rows, each with the row's updated_at.

    i_am is "requester" or "addressee"; statuses is a fixed SQL list such as
    "('pending')". Both come from code in this module, never from the client.
    """
    mine, theirs = ("requester_id", "addressee_id") if i_am == "requester" else ("addressee_id", "requester_id")
    return query(
        f"""SELECT a.id, a.username, a.first_name, a.last_name,
                   f.created_at, f.updated_at, f.{i_am}_hides_lifts AS hide_my_lifts
              FROM workout.friendship f
              JOIN workout.athlete a ON a.id = f.{theirs}
             WHERE f.{mine} = :me AND f.status IN {statuses}""",
        me=me,
    )


def _as_athlete_entry(row: dict, date_key: str, date) -> dict:
    athlete = {k: row[k] for k in ("id", "username", "first_name", "last_name")}
    return {"athlete": athlete, date_key: date}


# ---------- Friends ----------

def list_friends(req):
    me = req["athlete_id"]
    rows = _others(me, "requester", "('accepted')") + _others(me, "addressee", "('accepted')")
    rows.sort(key=_name_key)
    return respond(200, [
        {**_as_athlete_entry(r, "since", r["updated_at"]), "hide_my_lifts": bool(r["hide_my_lifts"])}
        for r in rows
    ])


def remove_friend(req):
    friend_id = path_id(req)
    me = req["athlete_id"]

    def work(tx: Tx):
        accepted = [r for r in _pair_rows(tx, me, friend_id) if r["status"] == "accepted"]
        if not accepted:
            raise HttpError(404, "Friend not found")
        for r in accepted:
            _delete_row(tx, r["requester_id"], r["addressee_id"])

    transaction(work)
    return respond(200, {"deleted": friend_id})


def set_hide_lifts(req):
    friend_id = path_id(req)
    me = req["athlete_id"]
    body = req["body"]
    require_fields(body, "hide_my_lifts")
    hide = body["hide_my_lifts"]
    if not isinstance(hide, bool):
        raise HttpError(400, "hide_my_lifts must be true or false")

    def work(tx: Tx):
        accepted = [r for r in _pair_rows(tx, me, friend_id) if r["status"] == "accepted"]
        if not accepted:
            raise HttpError(404, "Friend not found")
        row = accepted[0]
        # My side of the row. updated_at is left alone: it is when we became friends.
        column = "requester_hides_lifts" if row["requester_id"] == me else "addressee_hides_lifts"
        tx.query(
            f"""UPDATE workout.friendship SET {column} = :hide
                 WHERE requester_id = :req AND addressee_id = :addr""",
            hide=hide, req=row["requester_id"], addr=row["addressee_id"],
        )
        return {"athlete": _public_profile(friend_id, tx), "hide_my_lifts": hide}

    return respond(200, transaction(work))


# ---------- Friend requests ----------

def list_requests(req):
    me = req["athlete_id"]
    incoming = _others(me, "addressee", "('pending')")
    # A declined request looks the same as a pending one to its sender.
    outgoing = _others(me, "requester", "('pending', 'declined')")
    for rows in (incoming, outgoing):
        rows.sort(key=_created, reverse=True)
    return respond(200, {
        "incoming": [_as_athlete_entry(r, "sent_at", _created(r)) for r in incoming],
        "outgoing": [_as_athlete_entry(r, "sent_at", _created(r)) for r in outgoing],
    })


def send_request(req):
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
            raise HttpError(400, "You can't friend yourself")

        rows = _pair_rows(tx, me, other["id"])
        # Blocks come before every other rule. Being blocked looks like no such username.
        if any(r["status"] == "blocked" and r["requester_id"] == me for r in rows):
            raise HttpError(409, "Unblock them first")
        if any(r["status"] == "blocked" and r["requester_id"] == other["id"] for r in rows):
            raise HttpError(404, "No athlete with that username")
        mine = next((r for r in rows if r["requester_id"] == me), None)
        theirs = next((r for r in rows if r["requester_id"] == other["id"]), None)

        if any(r["status"] == "accepted" for r in rows):
            raise HttpError(409, "Already friends")

        if theirs and theirs["status"] == "pending":
            # They asked first: accepting is one step. Keep one row per pair.
            if mine:
                _delete_row(tx, me, other["id"])
            _accept(tx, other["id"], me)
            return {"status": "accepted", "athlete": other}

        if mine:
            raise HttpError(409, "Request declined" if mine["status"] == "declined" else "Request already sent")

        if theirs:  # You declined their earlier request: replace it with yours.
            _delete_row(tx, other["id"], me)
        tx.query(
            """INSERT INTO workout.friendship
                 (requester_id, addressee_id, status, created_at, updated_at,
                  requester_hides_lifts, addressee_hides_lifts)
               VALUES (:me, :other, 'pending', now(), now(), false, false)""",
            me=me, other=other["id"],
        )
        return {"status": "pending", "athlete": other}

    return respond(201, transaction(work))


def answer_request(req):
    requester_id = path_id(req)  # the athlete who sent the request
    me = req["athlete_id"]
    body = req["body"]
    require_fields(body, "status")
    status = body["status"]
    if status not in ANSWERS:
        raise HttpError(400, f"status must be one of: {', '.join(ANSWERS)}")

    def work(tx: Tx):
        rows = tx.query(
            """SELECT status FROM workout.friendship
                WHERE requester_id = :req AND addressee_id = :me""",
            req=requester_id, me=me,
        )
        # A block is never revealed, so it looks like there is no request.
        if not rows or rows[0]["status"] == "blocked":
            raise HttpError(404, "Request not found")
        if rows[0]["status"] != "pending":
            raise HttpError(409, "Request already answered")
        tx.query(
            """UPDATE workout.friendship SET status = :status, updated_at = now()
                WHERE requester_id = :req AND addressee_id = :me AND status = 'pending'""",
            status=status, req=requester_id, me=me,
        )
        return {"status": status, "athlete": _public_profile(requester_id, tx)}

    return respond(200, transaction(work))


def cancel_request(req):
    addressee_id = path_id(req)
    me = req["athlete_id"]

    def work(tx: Tx):
        rows = tx.query(
            """SELECT status FROM workout.friendship
                WHERE requester_id = :me AND addressee_id = :addr
                  AND status IN ('pending', 'declined')""",
            me=me, addr=addressee_id,
        )
        if not rows:
            raise HttpError(404, "Request not found")
        _delete_row(tx, me, addressee_id)

    transaction(work)
    return respond(200, {"deleted": addressee_id})


# ---------- Blocking ----------

def list_blocks(req):
    rows = _others(req["athlete_id"], "requester", "('blocked')")
    rows.sort(key=_created, reverse=True)
    return respond(200, [_as_athlete_entry(r, "since", _created(r)) for r in rows])


def _block(me: int, other_id: int) -> dict:
    """Block athlete other_id (one transaction) and return their public profile."""
    if other_id == me:
        raise HttpError(400, "You can't block yourself")
    # Any coaching between us ends, so programs one sent the other stop being kept in sync.
    detach_link(me, other_id)
    detach_link(other_id, me)

    def work(tx: Tx):
        athlete = _public_profile(other_id, tx)  # 404 if there is no such athlete
        rows = _pair_rows(tx, me, other_id)
        if any(r["status"] == "blocked" and r["requester_id"] == me for r in rows):
            return athlete  # already blocked
        for r in rows:
            # Their block of me stays: blocking them back must not lift it.
            if r["status"] == "blocked":
                continue
            _delete_row(tx, r["requester_id"], r["addressee_id"])
        # Blocking ends coaching in both directions too.
        tx.query(
            """DELETE FROM workout.coaching
                WHERE (coach_id = :me AND athlete_id = :other) OR (coach_id = :other AND athlete_id = :me)""",
            me=me, other=other_id,
        )
        tx.query(
            """DELETE FROM workout.coach_group_member
                WHERE (coach_id = :me AND athlete_id = :other) OR (coach_id = :other AND athlete_id = :me)""",
            me=me, other=other_id,
        )
        tx.query(
            """INSERT INTO workout.friendship
                 (requester_id, addressee_id, status, created_at, updated_at,
                  requester_hides_lifts, addressee_hides_lifts)
               VALUES (:me, :other, 'blocked', now(), now(), false, false)""",
            me=me, other=other_id,
        )
        return athlete

    return transaction(work)


def block_athlete(req):
    return respond(200, {"athlete": _block(req["athlete_id"], path_id(req))})


def block_by_username(req):
    me = req["athlete_id"]
    body = req["body"]
    require_fields(body, "username")
    username = to_text(body["username"], "username")
    found = query("SELECT id FROM workout.athlete WHERE lower(username) = lower(:username)", username=username)
    if not found:
        raise HttpError(404, "No athlete with that username")
    return respond(200, {"athlete": _block(me, found[0]["id"])})


def unblock_athlete(req):
    other_id = path_id(req)
    me = req["athlete_id"]

    def work(tx: Tx):
        rows = tx.query(
            """SELECT status FROM workout.friendship
                WHERE requester_id = :me AND addressee_id = :other AND status = 'blocked'""",
            me=me, other=other_id,
        )
        if not rows:
            raise HttpError(404, "Block not found")
        _delete_row(tx, me, other_id)

    transaction(work)
    return respond(200, {"deleted": other_id})


# ---------- A friend's data (read-only) ----------

def get_friend(req):
    friend_id = path_id(req)
    require_friend(friend_id, req["athlete_id"])
    return respond(200, _public_profile(friend_id))


def list_friend_lifts(req):
    friend_id = path_id(req)
    require_friend(friend_id, req["athlete_id"])
    if not lifts_visible(friend_id, req["athlete_id"]):
        return respond(200, [])  # same as a friend with no lifts
    rows = query(
        "SELECT id, date FROM workout.lift WHERE athlete_id = :friend ORDER BY date DESC, id DESC",
        friend=friend_id,
    )
    return respond(200, rows)


def get_friend_lift(req):
    friend_id = path_id(req)
    require_friend(friend_id, req["athlete_id"])
    if not lifts_visible(friend_id, req["athlete_id"]):
        raise HttpError(404, "Lift not found")  # same as a lift that doesn't exist
    lift = friend_lift(int(req["params"]["lift_id"]), friend_id)
    return respond(200, _lift_detail(lift))


def list_friend_programs(req):
    friend_id = path_id(req)
    require_friend(friend_id, req["athlete_id"])
    rows = query(
        """SELECT id, name, start_date, end_date FROM workout.program
            WHERE athlete_id = :friend AND is_public = true ORDER BY id DESC""",
        friend=friend_id,
    )
    return respond(200, rows)


def get_friend_program(req):
    friend_id = path_id(req)
    require_friend(friend_id, req["athlete_id"])
    program = friend_program(int(req["params"]["program_id"]), friend_id)
    return respond(200, _program_detail(program))


def copy_friend_program(req):
    friend_id = path_id(req)
    program_id = int(req["params"]["program_id"])
    me = req["athlete_id"]

    def work(tx: Tx):
        require_friend(friend_id, me, tx)
        source = friend_program(program_id, friend_id, tx)  # only if they made it public
        friend = _public_profile(friend_id, tx)
        name = " ".join(p for p in (friend["first_name"], friend["last_name"]) if p) or friend["username"]
        return copy_program(tx, program_id, me, f"{source['name']} (from {name})")

    return respond(201, _program_detail(transaction(work)))
