"""GET/PUT /me — the caller's own profile."""

from ..db import query
from ..responses import respond
from ..rows import update_row
from ..validation import to_date, to_int, to_text

PROFILE_COLUMNS = "id, username, first_name, last_name, dob, gender, height"
# All that anyone else ever sees of an athlete. Never return cognito_sub.
PUBLIC_PROFILE_COLUMNS = "id, username, first_name, last_name"


def get_me(req):
    [me] = query(
        f"SELECT {PROFILE_COLUMNS} FROM workout.athlete WHERE id = :me",
        me=req["athlete_id"],
    )
    return respond(200, me)


def update_me(req):
    update_row("athlete", req["athlete_id"], req["body"], {
        "first_name": (to_text, False),
        "last_name":  (to_text, False),
        "dob":        (to_date, False),
        "gender":     (to_text, False),
        "height":     (to_int, False),
    })
    return get_me(req)  # return the public fields only, not cognito_sub
