"""GET/PUT /me — the caller's own profile, including a changeable username."""

import pg8000.native as pg

from ..config import UNIQUE_VIOLATION
from ..db import pg_code, query
from ..responses import HttpError, respond
from ..rows import update_row
from ..validation import to_date, to_int, to_text, to_username

PROFILE_COLUMNS = "id, username, first_name, last_name, dob, gender, height"
# All that anyone else ever sees of an athlete. Never return cognito_sub.
PUBLIC_PROFILE_COLUMNS = "id, username, first_name, last_name"


def get_me(req):
    [me] = query(
        f"SELECT {PROFILE_COLUMNS} FROM workout.athlete WHERE id = :me",
        me=req["athlete_id"],
    )
    return respond(200, me)


def _set_username(me: int, value) -> None:
    """Change the caller's username. Stored lowercase, and unique (409 if taken)."""
    username = to_username(value)
    taken = query(
        "SELECT id FROM workout.athlete WHERE lower(username) = :username AND id <> :me",
        username=username, me=me,
    )
    try:
        if taken:
            raise HttpError(409, "That username is taken")
        query("UPDATE workout.athlete SET username = :username WHERE id = :me",
              username=username, me=me)
    except pg.DatabaseError as err:
        # Someone took it between the check and the update.
        if pg_code(err) != UNIQUE_VIOLATION:
            raise
        raise HttpError(409, "That username is taken")


def update_me(req):
    body = dict(req["body"])
    has_username = "username" in body
    if has_username:
        _set_username(req["athlete_id"], body.pop("username"))
    # A body with only a username has nothing else to update.
    if body or not has_username:
        update_row("athlete", req["athlete_id"], body, {
            "first_name": (to_text, False),
            "last_name":  (to_text, False),
            "dob":        (to_date, False),
            "gender":     (to_text, False),
            "height":     (to_int, False),
        })
    return get_me(req)  # return the public fields only, not cognito_sub
