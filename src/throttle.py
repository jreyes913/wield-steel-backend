"""Per-athlete limits on the routes that can be abused.

Each attempt is one row in workout.rate_limit. Rows older than the window are deleted
whenever that athlete and action are checked (DSQL has no TTL). Attempts count even when
they fail validation, since probing for usernames is the thing being limited.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pg8000.native as pg

from .config import RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_MINUTES, UNDEFINED_TABLE
from .db import pg_code, query
from .logs import log_event
from .responses import HttpError

# (method, route pattern) -> action name. PUT /me counts only when it changes the username.
THROTTLED = {
    ("POST", "/friend-requests"): "friend-request",
    ("POST", "/blocks"): "block",
    ("PUT", "/blocks/{id}"): "block",
    ("POST", "/exercises"): "exercise",
    ("PUT", "/me"): "username",
}


def action_for(method: str, pattern: str, body: dict) -> str | None:
    """The throttle action a request counts against, or None if it isn't limited."""
    action = THROTTLED.get((method, pattern))
    if action == "username" and "username" not in body:
        return None
    return action


def check(athlete_id: int, action: str) -> None:
    """Record an attempt, or raise 429 if the athlete has made too many in the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    try:
        query(
            """DELETE FROM workout.rate_limit
                WHERE athlete_id = :me AND action = :action
                  AND created_at < :cutoff""",
            me=athlete_id, action=action, cutoff=cutoff,
        )
        [row] = query(
            "SELECT COUNT(*) AS n FROM workout.rate_limit WHERE athlete_id = :me AND action = :action",
            me=athlete_id, action=action,
        )
        if row["n"] >= RATE_LIMIT_MAX:
            raise HttpError(429, "Too many attempts. Please try again later.")
        query(
            "INSERT INTO workout.rate_limit (athlete_id, action, created_at) VALUES (:me, :action, now())",
            me=athlete_id, action=action,
        )
    except pg.DatabaseError as err:
        # Table not created yet: let the request through rather than break these routes.
        if pg_code(err) != UNDEFINED_TABLE:
            raise
        log_event("rate_limit_table_missing")

