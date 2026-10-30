"""Workout Tracker API — one Lambda, many endpoints.

Runs behind API Gateway (HTTP API) and talks to Aurora DSQL.

Environment variables (set in the Lambda console):
    DSQL_ENDPOINT   e.g. abc123.dsql.us-east-2.on.aws
    AWS_REGION      set automatically by Lambda

Every request must come through API Gateway with a valid Cognito login.
The caller's identity is read from the token, never from the request body,
so a user can only ever see or change their own data. Anything that isn't
yours returns 404, so other people's IDs aren't revealed.

Endpoints
---------
Profile
    GET    /me                          your profile (created on first call)
    PUT    /me                          {first_name?, last_name?, dob?, gender?, height?}

Reference data (shared by everyone)
    GET    /muscle-groups
    GET    /exercises                   optional ?muscle_group_id=1
    POST   /exercises                   {name, muscle_group_id, difficulty?, complexity?}

Logged workouts
    GET    /lifts                       your lifts, newest first
    POST   /lifts                       {date, body_weight?}
    GET    /lifts/{id}                  one lift with its sets and subsets
    PUT    /lifts/{id}                  {date?, body_weight?}
    DELETE /lifts/{id}                  deletes the lift, its sets and subsets
    POST   /lifts/{id}/sets             {position, subsets: [{exercise_id, position, reps?, rir?, weight?}]}
    POST   /lifts/{id}/apply            {program_day_id}  copy a program day into the lift
    PUT    /sets/{id}                   {position}
    DELETE /sets/{id}                   deletes the set and its subsets
    POST   /sets/{id}/subsets           {exercise_id, position, reps?, rir?, weight?}
    PUT    /subsets/{id}                {exercise_id?, position?, reps?, rir?, weight?}
    DELETE /subsets/{id}

Progress
    GET    /progress/body-weight        optional ?start=YYYY-MM-DD&end=YYYY-MM-DD
    GET    /progress/strength           ?exercise_id=1, optional start/end

Programs (templates). A program has days, days have sets, sets have subsets.
    GET    /programs                    your programs
    POST   /programs                    {name, start_date?, end_date?}
    GET    /programs/{id}               the full tree: days -> sets -> subsets
    PUT    /programs/{id}               {name?, start_date?, end_date?}
    DELETE /programs/{id}               deletes everything under it
    POST   /programs/{id}/days          {name}
    PUT    /program-days/{id}           {name}
    DELETE /program-days/{id}
    POST   /program-days/{id}/sets      {position, subsets: [{exercise_id, position, reps?}]}
    PUT    /program-sets/{id}           {position}
    DELETE /program-sets/{id}
    POST   /program-sets/{id}/subsets   {exercise_id, position, reps?}
    PUT    /program-subsets/{id}        {exercise_id?, position?, reps?}
    DELETE /program-subsets/{id}

Notes on Aurora DSQL
--------------------
* No foreign keys: parents are checked before inserting children, and
  deletes remove children first, inside one transaction.
* Queries are kept to simple shapes (plain JOINs, one-row lookups, deletes
  by a single id) because DSQL rejects some PostgreSQL query forms.

Handler setting in Lambda: lambda_function.handler
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

import boto3
import pg8000.native as pg

log = logging.getLogger()
log.setLevel(logging.INFO)

DSQL_ENDPOINT = os.environ.get("DSQL_ENDPOINT", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")

# DSQL closes connections after 1 hour; recycle well before that.
MAX_CONN_AGE_SECONDS = 50 * 60
SERIALIZATION_FAILURE = "40001"  # DSQL optimistic-concurrency conflict -> retry
UNIQUE_VIOLATION = "23505"


# ======================================================================
# Database connection
# ======================================================================
# The connection lives at module level, so it is reused across invocations
# while the Lambda instance stays warm. Each new connection gets a fresh IAM
# token from the function's execution role — no stored password.

_dsql = boto3.client("dsql", region_name=AWS_REGION)
_conn: pg.Connection | None = None
_conn_opened_at = 0.0


def _connect() -> pg.Connection:
    token = _dsql.generate_db_connect_admin_auth_token(
        Hostname=DSQL_ENDPOINT, Region=AWS_REGION
    )
    return pg.Connection(
        user="admin",
        password=token,
        host=DSQL_ENDPOINT,
        port=5432,
        database="postgres",
        ssl_context=ssl.create_default_context(),
        timeout=10,
    )


def get_conn() -> pg.Connection:
    global _conn, _conn_opened_at
    if _conn is None or time.time() - _conn_opened_at > MAX_CONN_AGE_SECONDS:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = _connect()
        _conn_opened_at = time.time()
    return _conn


def _reset_conn() -> None:
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _rows_as_dicts(conn: pg.Connection, rows: list | None) -> list[dict]:
    # Statements that return no rows (DELETE, INSERT without RETURNING) give None.
    if not rows:
        return []
    cols = [c["name"] for c in (conn.columns or [])]
    return [dict(zip(cols, row)) for row in rows]


def _pg_code(err: Exception) -> str | None:
    if isinstance(err, pg.DatabaseError) and err.args and isinstance(err.args[0], dict):
        return err.args[0].get("C")
    return None


def with_retry(fn: Callable[[pg.Connection], Any], attempts: int = 3) -> Any:
    """Run fn(conn), retrying on DSQL conflicts and reconnecting on dropped connections."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(get_conn())
        except pg.InterfaceError:
            # Connection went stale (e.g. idle too long) — reconnect and try again.
            _reset_conn()
            if attempt == attempts:
                raise
        except pg.DatabaseError as err:
            if _pg_code(err) != SERIALIZATION_FAILURE or attempt == attempts:
                raise


def query(sql: str, **params) -> list[dict]:
    """Run one statement and return rows as dicts. Use :name placeholders."""
    def run(conn: pg.Connection):
        rows = conn.run(sql, **params)
        return _rows_as_dicts(conn, rows)
    return with_retry(run)


class Tx:
    """Gives code inside a transaction the same query() style."""

    def __init__(self, conn: pg.Connection):
        self._conn = conn

    def query(self, sql: str, **params) -> list[dict]:
        rows = self._conn.run(sql, **params)
        return _rows_as_dicts(self._conn, rows)


def transaction(fn: Callable[[Tx], Any]) -> Any:
    """Run several statements as one all-or-nothing transaction (retried on conflict)."""
    def run(conn: pg.Connection):
        conn.run("BEGIN")
        try:
            result = fn(Tx(conn))
            conn.run("COMMIT")
            return result
        except Exception:
            try:
                conn.run("ROLLBACK")
            except Exception:
                pass
            raise
    return with_retry(run)


def _runner(db: Tx | None) -> Callable[..., list[dict]]:
    """Use the transaction if one is given, otherwise a standalone query."""
    return db.query if db else query


# ======================================================================
# HTTP helpers and input conversion
# ======================================================================

class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json_default(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Not JSON serializable: {type(value)}")


def respond(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def require_fields(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) is None]
    if missing:
        raise HttpError(400, f"Missing field(s): {', '.join(missing)}")


def to_date(value: Any, field: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"{field} must be YYYY-MM-DD")


def to_int(value: Any, field: str = "value") -> int:
    if isinstance(value, bool):
        raise HttpError(400, f"{field} must be a whole number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"{field} must be a whole number")
    if not number.is_integer():
        raise HttpError(400, f"{field} must be a whole number")
    return int(number)


def to_float(value: Any, field: str = "value") -> float:
    if isinstance(value, bool):
        raise HttpError(400, f"{field} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"{field} must be a number")


def to_text(value: Any, field: str = "value") -> str:
    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, f"{field} must be non-empty text")
    return value.strip()


def optional(convert: Callable, value: Any, field: str):
    """Convert a value that is allowed to be missing or null."""
    return None if value is None else convert(value, field)


def path_id(req: dict) -> int:
    return int(req["params"]["id"])


def assert_exists(table: str, row_id: Any, db: Tx | None = None) -> None:
    """DSQL has no foreign keys, so check referenced rows before writing.

    `table` only ever comes from code in this file, never from user input.
    """
    rows = _runner(db)(f"SELECT 1 AS ok FROM workout.{table} WHERE id = :id", id=row_id)
    if not rows:
        raise HttpError(400, f"{table} {row_id} does not exist")


def update_row(table: str, row_id: int, body: dict, fields: dict, db: Tx | None = None) -> dict:
    """Update only the fields present in the body, then return the row.

    fields maps column name -> (converter, required). A required column can
    be changed but not cleared. Column names come from the fields dict in
    this file, never from the client.
    """
    updates = {}
    for column, (convert, required) in fields.items():
        if column not in body:
            continue
        value = body[column]
        if value is None:
            if required:
                raise HttpError(400, f"{column} cannot be empty")
            updates[column] = None
        else:
            updates[column] = convert(value, column)
    if not updates:
        raise HttpError(400, f"Send at least one of: {', '.join(fields)}")

    set_clause = ", ".join(f"{col} = :{col}" for col in updates)
    rows = _runner(db)(
        f"UPDATE workout.{table} SET {set_clause} WHERE id = :row_id RETURNING *",
        row_id=row_id,
        **updates,
    )
    return rows[0]


# ======================================================================
# Who is calling, and do they own this?
# ======================================================================
# API Gateway's JWT authorizer has already verified the Cognito token before
# this code runs; we just read the verified claims it passes along.

_athlete_cache: dict[str, int] = {}


def _claims(event: dict) -> dict:
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
            if _pg_code(err) != UNIQUE_VIOLATION:
                raise
            rows = query("SELECT id FROM workout.athlete WHERE cognito_sub = :sub", sub=sub)

    _athlete_cache[sub] = rows[0]["id"]
    return rows[0]["id"]


def _owned(sql: str, row_id: int, me: int, label: str, db: Tx | None) -> dict:
    rows = _runner(db)(sql, id=row_id, me=me)
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


# ======================================================================
# Child-first deletes (DSQL has no ON DELETE CASCADE)
# ======================================================================
# Each helper deletes one parent and everything under it, using single-id
# statements. Call them inside transaction() so it's all or nothing.

def _delete_set_tree(tx: Tx, set_id: int) -> None:
    tx.query("DELETE FROM workout.exercise_subset WHERE exercise_set_id = :id", id=set_id)
    tx.query("DELETE FROM workout.exercise_set WHERE id = :id", id=set_id)


def _delete_lift_tree(tx: Tx, lift_id: int) -> None:
    for s in tx.query("SELECT id FROM workout.exercise_set WHERE lift_id = :id", id=lift_id):
        _delete_set_tree(tx, s["id"])
    tx.query("DELETE FROM workout.lift WHERE id = :id", id=lift_id)


def _delete_program_set_tree(tx: Tx, set_id: int) -> None:
    tx.query(
        "DELETE FROM workout.program_exercise_subset WHERE program_exercise_set_id = :id",
        id=set_id,
    )
    tx.query("DELETE FROM workout.program_exercise_set WHERE id = :id", id=set_id)


def _delete_program_day_tree(tx: Tx, day_id: int) -> None:
    for s in tx.query(
        "SELECT id FROM workout.program_exercise_set WHERE program_lift_id = :id", id=day_id
    ):
        _delete_program_set_tree(tx, s["id"])
    tx.query("DELETE FROM workout.program_lift WHERE id = :id", id=day_id)


def _delete_program_tree(tx: Tx, program_id: int) -> None:
    for d in tx.query(
        "SELECT id FROM workout.program_lift WHERE program_id = :id", id=program_id
    ):
        _delete_program_day_tree(tx, d["id"])
    tx.query("DELETE FROM workout.program WHERE id = :id", id=program_id)


# ======================================================================
# Profile
# ======================================================================

PROFILE_COLUMNS = "id, username, first_name, last_name, dob, gender, height"


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


# ======================================================================
# Reference data
# ======================================================================

def list_muscle_groups(req):
    return respond(200, query("SELECT id, name FROM workout.muscle_group ORDER BY name"))


def list_exercises(req):
    mg = req["query"].get("muscle_group_id")
    if mg:
        rows = query(
            "SELECT * FROM workout.exercise WHERE muscle_group_id = :mg ORDER BY name",
            mg=to_int(mg, "muscle_group_id"),
        )
    else:
        rows = query("SELECT * FROM workout.exercise ORDER BY name")
    return respond(200, rows)


def create_exercise(req):
    body = req["body"]
    require_fields(body, "name", "muscle_group_id")
    mg = to_int(body["muscle_group_id"], "muscle_group_id")
    assert_exists("muscle_group", mg)
    [row] = query(
        """INSERT INTO workout.exercise (name, muscle_group_id, difficulty, complexity)
           VALUES (:name, :mg, :difficulty, :complexity) RETURNING *""",
        name=to_text(body["name"], "name"),
        mg=mg,
        difficulty=optional(to_float, body.get("difficulty"), "difficulty"),
        complexity=optional(to_text, body.get("complexity"), "complexity"),
    )
    return respond(201, row)


# ======================================================================
# Lifts
# ======================================================================

def _lift_detail(lift: dict) -> dict:
    """A lift with its sets and subsets nested, in position order."""
    rows = query(
        """SELECT s.id AS set_id, s.position AS set_position,
                  sub.id AS subset_id, sub.position AS subset_position,
                  sub.reps, sub.rir, sub.weight,
                  e.id AS exercise_id, e.name AS exercise_name
             FROM workout.exercise_set s
             LEFT JOIN workout.exercise_subset sub ON sub.exercise_set_id = s.id
             LEFT JOIN workout.exercise e ON e.id = sub.exercise_id
            WHERE s.lift_id = :id
            ORDER BY s.position, s.id, sub.position, sub.id""",
        id=lift["id"],
    )
    sets: dict[int, dict] = {}
    for r in rows:
        s = sets.setdefault(
            r["set_id"], {"id": r["set_id"], "position": r["set_position"], "subsets": []}
        )
        if r["subset_id"] is not None:
            s["subsets"].append({
                "id": r["subset_id"],
                "position": r["subset_position"],
                "exercise": {"id": r["exercise_id"], "name": r["exercise_name"]},
                "reps": r["reps"],
                "rir": r["rir"],
                "weight": r["weight"],
            })
    return {**lift, "sets": list(sets.values())}


def list_my_lifts(req):
    rows = query(
        "SELECT * FROM workout.lift WHERE athlete_id = :me ORDER BY date DESC, id DESC",
        me=req["athlete_id"],
    )
    return respond(200, rows)


def create_lift(req):
    body = req["body"]
    require_fields(body, "date")
    [row] = query(
        """INSERT INTO workout.lift (date, athlete_id, body_weight)
           VALUES (:date, :me, :bw) RETURNING *""",
        date=to_date(body["date"]),
        me=req["athlete_id"],  # always the caller — never taken from the body
        bw=optional(to_int, body.get("body_weight"), "body_weight"),
    )
    return respond(201, row)


def get_lift(req):
    lift = own_lift(path_id(req), req["athlete_id"])
    return respond(200, _lift_detail(lift))


def update_lift(req):
    lift_id = path_id(req)
    own_lift(lift_id, req["athlete_id"])
    row = update_row("lift", lift_id, req["body"], {
        "date":        (to_date, True),
        "body_weight": (to_int, False),
    })
    return respond(200, row)


def delete_lift(req):
    lift_id = path_id(req)

    def work(tx: Tx):
        own_lift(lift_id, req["athlete_id"], tx)
        _delete_lift_tree(tx, lift_id)

    transaction(work)
    return respond(200, {"deleted": lift_id})


# ---------- Sets and subsets in a lift ----------

SUBSET_FIELDS = {
    "exercise_id": (to_int, True),
    "position":    (to_int, True),
    "reps":        (to_int, False),
    "rir":         (to_int, False),
    "weight":      (to_float, False),
}


def _validate_subsets(subsets: Any) -> list[dict]:
    if subsets is None:
        return []
    if not isinstance(subsets, list):
        raise HttpError(400, "subsets must be a list")
    for i, sub in enumerate(subsets):
        if not isinstance(sub, dict) or sub.get("exercise_id") is None or sub.get("position") is None:
            raise HttpError(400, f"subsets[{i}] needs exercise_id and position")
    return subsets


def _insert_subset(tx: Tx, set_id: int, sub: dict) -> dict:
    exercise_id = to_int(sub["exercise_id"], "exercise_id")
    assert_exists("exercise", exercise_id, tx)
    return tx.query(
        """INSERT INTO workout.exercise_subset
             (exercise_set_id, exercise_id, position, reps, rir, weight)
           VALUES (:set_id, :ex, :pos, :reps, :rir, :weight) RETURNING *""",
        set_id=set_id,
        ex=exercise_id,
        pos=to_int(sub["position"], "position"),
        reps=optional(to_int, sub.get("reps"), "reps"),
        rir=optional(to_int, sub.get("rir"), "rir"),
        weight=optional(to_float, sub.get("weight"), "weight"),
    )[0]


def add_set_to_lift(req):
    lift_id = path_id(req)
    body = req["body"]
    require_fields(body, "position")
    position = to_int(body["position"], "position")
    subsets = _validate_subsets(body.get("subsets"))

    def work(tx: Tx):
        own_lift(lift_id, req["athlete_id"], tx)
        [new_set] = tx.query(
            """INSERT INTO workout.exercise_set (lift_id, position)
               VALUES (:lift_id, :pos) RETURNING *""",
            lift_id=lift_id,
            pos=position,
        )
        saved = [_insert_subset(tx, new_set["id"], sub) for sub in subsets]
        return {**new_set, "subsets": saved}

    return respond(201, transaction(work))


def update_set(req):
    set_id = path_id(req)
    own_set(set_id, req["athlete_id"])
    row = update_row("exercise_set", set_id, req["body"], {"position": (to_int, True)})
    return respond(200, row)


def delete_set(req):
    set_id = path_id(req)

    def work(tx: Tx):
        own_set(set_id, req["athlete_id"], tx)
        _delete_set_tree(tx, set_id)

    transaction(work)
    return respond(200, {"deleted": set_id})


def add_subset_to_set(req):
    set_id = path_id(req)
    body = req["body"]
    require_fields(body, "exercise_id", "position")

    def work(tx: Tx):
        own_set(set_id, req["athlete_id"], tx)
        return _insert_subset(tx, set_id, body)

    return respond(201, transaction(work))


def update_subset(req):
    subset_id = path_id(req)
    body = req["body"]
    own_subset(subset_id, req["athlete_id"])
    if body.get("exercise_id") is not None:
        assert_exists("exercise", to_int(body["exercise_id"], "exercise_id"))
    return respond(200, update_row("exercise_subset", subset_id, body, SUBSET_FIELDS))


def delete_subset(req):
    subset_id = path_id(req)
    own_subset(subset_id, req["athlete_id"])
    query("DELETE FROM workout.exercise_subset WHERE id = :id", id=subset_id)
    return respond(200, {"deleted": subset_id})


# ---------- Apply a program day to a lift ----------

def apply_program_day(req):
    lift_id = path_id(req)
    body = req["body"]
    require_fields(body, "program_day_id")
    day_id = to_int(body["program_day_id"], "program_day_id")
    me = req["athlete_id"]

    def work(tx: Tx):
        lift = own_lift(lift_id, me, tx)
        own_program_day(day_id, me, tx)

        # Append after any sets already in the lift so positions don't collide.
        max_pos = tx.query(
            "SELECT COALESCE(MAX(position), 0) AS max_pos FROM workout.exercise_set WHERE lift_id = :id",
            id=lift_id,
        )[0]["max_pos"]
        template_sets = tx.query(
            """SELECT id FROM workout.program_exercise_set
                WHERE program_lift_id = :id ORDER BY position, id""",
            id=day_id,
        )
        for i, tset in enumerate(template_sets, start=1):
            [new_set] = tx.query(
                """INSERT INTO workout.exercise_set (lift_id, position)
                   VALUES (:lift_id, :pos) RETURNING id""",
                lift_id=lift_id,
                pos=max_pos + i,
            )
            for tsub in tx.query(
                """SELECT exercise_id, position, reps FROM workout.program_exercise_subset
                    WHERE program_exercise_set_id = :id ORDER BY position, id""",
                id=tset["id"],
            ):
                # Reps come from the plan; weight and RIR are left empty to fill in.
                tx.query(
                    """INSERT INTO workout.exercise_subset
                         (exercise_set_id, exercise_id, position, reps, rir, weight)
                       VALUES (:set_id, :ex, :pos, :reps, NULL, NULL)""",
                    set_id=new_set["id"],
                    ex=tsub["exercise_id"],
                    pos=tsub["position"],
                    reps=tsub["reps"],
                )
        return lift

    lift = transaction(work)
    return respond(201, _lift_detail(lift))


# ======================================================================
# Progress
# ======================================================================

def _date_range_clause(req, column: str, params: dict) -> str:
    """Optional ?start= / ?end= filters, added only when given."""
    clause = ""
    q = req["query"]
    if q.get("start"):
        params["start"] = to_date(q["start"], "start")
        clause += f" AND {column} >= :start"
    if q.get("end"):
        params["end"] = to_date(q["end"], "end")
        clause += f" AND {column} <= :end"
    return clause


def progress_body_weight(req):
    params = {"me": req["athlete_id"]}
    where = _date_range_clause(req, "date", params)
    rows = query(
        f"""SELECT date, body_weight FROM workout.lift
             WHERE athlete_id = :me AND body_weight IS NOT NULL{where}
             ORDER BY date, id""",
        **params,
    )
    return respond(200, rows)


def estimate_1rm(weight: float, reps: int, rir: int | None) -> float | None:
    """Estimated one-rep max from a set. Only sets with RIR below 6 qualify."""
    if rir is None or rir >= 6:
        return None
    divisor = 30 if rir <= 3 else 40
    return weight * (1 + (reps + rir) / divisor)


def compute_strength(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Add 1RM, intensity, load and volume to each set, plus a per-day summary.

    rows must be ordered by date, then lift, set position, subset position.
      e1rm       estimated 1RM from this set (RIR < 6 only)
      daily_1rm  best e1rm that day
      intensity  weight / daily_1rm
      load       intensity * weight * reps / (1 + 0.5 * rir)
      volume     running total of load within the day
    Days where no set qualifies for a 1RM estimate get empty
    intensity, load and volume instead of an error.
    """
    sets = []
    for r in rows:
        weight, reps, rir = float(r["weight"]), int(r["reps"]), r["rir"]
        sets.append({
            "date": r["date"],
            "lift_id": r["lift_id"],
            "set_position": r["set_position"],
            "subset_position": r["subset_position"],
            "weight": weight,
            "reps": reps,
            "rir": rir,
            "e1rm": estimate_1rm(weight, reps, rir),
        })

    best: dict[date, float] = {}
    for s in sets:
        if s["e1rm"] is not None:
            best[s["date"]] = max(best.get(s["date"], 0.0), s["e1rm"])

    running: dict[date, float] = {}
    for s in sets:
        daily = best.get(s["date"])
        s["daily_1rm"] = daily
        if daily and s["rir"] is not None:
            s["intensity"] = s["weight"] / daily
            s["load"] = s["intensity"] * s["weight"] * s["reps"] / (1 + 0.5 * s["rir"])
            running[s["date"]] = running.get(s["date"], 0.0) + s["load"]
            s["volume"] = running[s["date"]]
        else:
            s["intensity"] = s["load"] = None
            s["volume"] = running.get(s["date"])

    days: dict[date, dict] = {}
    for s in sets:
        d = days.setdefault(s["date"], {
            "date": s["date"], "daily_1rm": s["daily_1rm"],
            "top_weight": 0.0, "set_count": 0, "volume": None,
        })
        d["top_weight"] = max(d["top_weight"], s["weight"])
        d["set_count"] += 1
        if s["volume"] is not None:
            d["volume"] = s["volume"]

    def rounded(item: dict) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in item.items()}

    return [rounded(s) for s in sets], [rounded(d) for d in days.values()]


def progress_strength(req):
    q = req["query"]
    if not q.get("exercise_id"):
        raise HttpError(400, "exercise_id is required")
    exercise_id = to_int(q["exercise_id"], "exercise_id")
    exercise = query("SELECT id, name FROM workout.exercise WHERE id = :id", id=exercise_id)
    if not exercise:
        raise HttpError(404, "Exercise not found")

    params = {"me": req["athlete_id"], "ex": exercise_id}
    where = _date_range_clause(req, "l.date", params)
    rows = query(
        f"""SELECT l.id AS lift_id, l.date,
                   s.position AS set_position, sub.position AS subset_position,
                   sub.weight, sub.reps, sub.rir
              FROM workout.lift l
              JOIN workout.exercise_set s ON s.lift_id = l.id
              JOIN workout.exercise_subset sub ON sub.exercise_set_id = s.id
             WHERE l.athlete_id = :me AND sub.exercise_id = :ex
               AND sub.weight IS NOT NULL AND sub.reps IS NOT NULL{where}
             ORDER BY l.date, l.id, s.position, s.id, sub.position, sub.id""",
        **params,
    )
    sets, days = compute_strength(rows)
    return respond(200, {"exercise": exercise[0], "days": days, "sets": sets})


# ======================================================================
# Programs
# ======================================================================

PROGRAM_FIELDS = {
    "name":       (to_text, True),
    "start_date": (to_date, False),
    "end_date":   (to_date, False),
}

PROGRAM_SUBSET_FIELDS = {
    "exercise_id": (to_int, True),
    "position":    (to_int, True),
    "reps":        (to_int, False),
}


def _program_detail(program: dict) -> dict:
    """A program with its days, sets and subsets nested, in order."""
    rows = query(
        """SELECT pl.id AS day_id, pl.name AS day_name,
                  ps.id AS set_id, ps.position AS set_position,
                  pss.id AS subset_id, pss.position AS subset_position, pss.reps,
                  e.id AS exercise_id, e.name AS exercise_name
             FROM workout.program_lift pl
             LEFT JOIN workout.program_exercise_set ps ON ps.program_lift_id = pl.id
             LEFT JOIN workout.program_exercise_subset pss ON pss.program_exercise_set_id = ps.id
             LEFT JOIN workout.exercise e ON e.id = pss.exercise_id
            WHERE pl.program_id = :id
            ORDER BY pl.id, ps.position, ps.id, pss.position, pss.id""",
        id=program["id"],
    )
    days: dict[int, dict] = {}
    sets: dict[int, dict] = {}
    for r in rows:
        day = days.setdefault(r["day_id"], {"id": r["day_id"], "name": r["day_name"], "sets": []})
        if r["set_id"] is None:
            continue
        if r["set_id"] not in sets:
            sets[r["set_id"]] = {"id": r["set_id"], "position": r["set_position"], "subsets": []}
            day["sets"].append(sets[r["set_id"]])
        if r["subset_id"] is not None:
            sets[r["set_id"]]["subsets"].append({
                "id": r["subset_id"],
                "position": r["subset_position"],
                "exercise": {"id": r["exercise_id"], "name": r["exercise_name"]},
                "reps": r["reps"],
            })
    return {**program, "days": list(days.values())}


def list_programs(req):
    rows = query(
        "SELECT * FROM workout.program WHERE athlete_id = :me ORDER BY id DESC",
        me=req["athlete_id"],
    )
    return respond(200, rows)


def create_program(req):
    body = req["body"]
    require_fields(body, "name")
    [row] = query(
        """INSERT INTO workout.program (athlete_id, name, start_date, end_date)
           VALUES (:me, :name, :start, :end) RETURNING *""",
        me=req["athlete_id"],
        name=to_text(body["name"], "name"),
        start=optional(to_date, body.get("start_date"), "start_date"),
        end=optional(to_date, body.get("end_date"), "end_date"),
    )
    return respond(201, row)


def get_program(req):
    program = own_program(path_id(req), req["athlete_id"])
    return respond(200, _program_detail(program))


def update_program(req):
    program_id = path_id(req)
    own_program(program_id, req["athlete_id"])
    return respond(200, update_row("program", program_id, req["body"], PROGRAM_FIELDS))


def delete_program(req):
    program_id = path_id(req)

    def work(tx: Tx):
        own_program(program_id, req["athlete_id"], tx)
        _delete_program_tree(tx, program_id)

    transaction(work)
    return respond(200, {"deleted": program_id})


# ---------- Program days ----------

def add_program_day(req):
    program_id = path_id(req)
    body = req["body"]
    require_fields(body, "name")
    own_program(program_id, req["athlete_id"])
    [row] = query(
        """INSERT INTO workout.program_lift (program_id, name)
           VALUES (:pid, :name) RETURNING *""",
        pid=program_id,
        name=to_text(body["name"], "name"),
    )
    return respond(201, row)


def update_program_day(req):
    day_id = path_id(req)
    own_program_day(day_id, req["athlete_id"])
    return respond(200, update_row("program_lift", day_id, req["body"], {"name": (to_text, True)}))


def delete_program_day(req):
    day_id = path_id(req)

    def work(tx: Tx):
        own_program_day(day_id, req["athlete_id"], tx)
        _delete_program_day_tree(tx, day_id)

    transaction(work)
    return respond(200, {"deleted": day_id})


# ---------- Program sets and subsets ----------

def _insert_program_subset(tx: Tx, set_id: int, sub: dict) -> dict:
    exercise_id = to_int(sub["exercise_id"], "exercise_id")
    assert_exists("exercise", exercise_id, tx)
    return tx.query(
        """INSERT INTO workout.program_exercise_subset
             (program_exercise_set_id, exercise_id, position, reps)
           VALUES (:set_id, :ex, :pos, :reps) RETURNING *""",
        set_id=set_id,
        ex=exercise_id,
        pos=to_int(sub["position"], "position"),
        reps=optional(to_int, sub.get("reps"), "reps"),
    )[0]


def add_program_set(req):
    day_id = path_id(req)
    body = req["body"]
    require_fields(body, "position")
    position = to_int(body["position"], "position")
    subsets = _validate_subsets(body.get("subsets"))

    def work(tx: Tx):
        own_program_day(day_id, req["athlete_id"], tx)
        [new_set] = tx.query(
            """INSERT INTO workout.program_exercise_set (program_lift_id, position)
               VALUES (:day_id, :pos) RETURNING *""",
            day_id=day_id,
            pos=position,
        )
        saved = [_insert_program_subset(tx, new_set["id"], sub) for sub in subsets]
        return {**new_set, "subsets": saved}

    return respond(201, transaction(work))


def update_program_set(req):
    set_id = path_id(req)
    own_program_set(set_id, req["athlete_id"])
    return respond(200, update_row("program_exercise_set", set_id, req["body"], {"position": (to_int, True)}))


def delete_program_set(req):
    set_id = path_id(req)

    def work(tx: Tx):
        own_program_set(set_id, req["athlete_id"], tx)
        _delete_program_set_tree(tx, set_id)

    transaction(work)
    return respond(200, {"deleted": set_id})


def add_program_subset(req):
    set_id = path_id(req)
    body = req["body"]
    require_fields(body, "exercise_id", "position")

    def work(tx: Tx):
        own_program_set(set_id, req["athlete_id"], tx)
        return _insert_program_subset(tx, set_id, body)

    return respond(201, transaction(work))


def update_program_subset(req):
    subset_id = path_id(req)
    body = req["body"]
    own_program_subset(subset_id, req["athlete_id"])
    if body.get("exercise_id") is not None:
        assert_exists("exercise", to_int(body["exercise_id"], "exercise_id"))
    return respond(200, update_row("program_exercise_subset", subset_id, body, PROGRAM_SUBSET_FIELDS))


def delete_program_subset(req):
    subset_id = path_id(req)
    own_program_subset(subset_id, req["athlete_id"])
    query("DELETE FROM workout.program_exercise_subset WHERE id = :id", id=subset_id)
    return respond(200, {"deleted": subset_id})


# ======================================================================
# Router
# ======================================================================
# Each route is METHOD + path pattern. {id} captures a number from the URL.
# To add an endpoint: write a handler(req) above and add one line here.

def _compile(pattern: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>\\d+)", pattern) + "/?$")


ROUTES = [
    # Profile
    ("GET",    "/me",                         get_me),
    ("PUT",    "/me",                         update_me),
    # Reference data
    ("GET",    "/muscle-groups",              list_muscle_groups),
    ("GET",    "/exercises",                  list_exercises),
    ("POST",   "/exercises",                  create_exercise),
    # Lifts
    ("GET",    "/lifts",                      list_my_lifts),
    ("POST",   "/lifts",                      create_lift),
    ("GET",    "/lifts/{id}",                 get_lift),
    ("PUT",    "/lifts/{id}",                 update_lift),
    ("DELETE", "/lifts/{id}",                 delete_lift),
    ("POST",   "/lifts/{id}/sets",            add_set_to_lift),
    ("POST",   "/lifts/{id}/apply",           apply_program_day),
    ("PUT",    "/sets/{id}",                  update_set),
    ("DELETE", "/sets/{id}",                  delete_set),
    ("POST",   "/sets/{id}/subsets",          add_subset_to_set),
    ("PUT",    "/subsets/{id}",               update_subset),
    ("DELETE", "/subsets/{id}",               delete_subset),
    # Progress
    ("GET",    "/progress/body-weight",       progress_body_weight),
    ("GET",    "/progress/strength",          progress_strength),
    # Programs
    ("GET",    "/programs",                   list_programs),
    ("POST",   "/programs",                   create_program),
    ("GET",    "/programs/{id}",              get_program),
    ("PUT",    "/programs/{id}",              update_program),
    ("DELETE", "/programs/{id}",              delete_program),
    ("POST",   "/programs/{id}/days",         add_program_day),
    ("PUT",    "/program-days/{id}",          update_program_day),
    ("DELETE", "/program-days/{id}",          delete_program_day),
    ("POST",   "/program-days/{id}/sets",     add_program_set),
    ("PUT",    "/program-sets/{id}",          update_program_set),
    ("DELETE", "/program-sets/{id}",          delete_program_set),
    ("POST",   "/program-sets/{id}/subsets",  add_program_subset),
    ("PUT",    "/program-subsets/{id}",       update_program_subset),
    ("DELETE", "/program-subsets/{id}",       delete_program_subset),
]
ROUTES = [(method, _compile(path), fn) for method, path, fn in ROUTES]


def _parse_body(event: dict) -> dict:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HttpError(400, "Body must be valid JSON")
    if not isinstance(body, dict):
        raise HttpError(400, "Body must be a JSON object")
    return body


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")

    # Browser CORS preflight. Browsers never attach the token to it, so it must
    # be answered before the sign-in check below. API Gateway adds the CORS
    # headers itself from the API's CORS settings.
    if method == "OPTIONS":
        return {"statusCode": 204}

    try:
        claims = _claims(event)  # 401 if not signed in
        path_matched = False
        for route_method, regex, fn in ROUTES:
            match = regex.match(path)
            if not match:
                continue
            path_matched = True
            if route_method == method:
                return fn({
                    "athlete_id": current_athlete_id(claims),
                    "params": match.groupdict(),
                    "query": event.get("queryStringParameters") or {},
                    "body": _parse_body(event),
                })
        if path_matched:
            return respond(405, {"error": f"{method} not allowed on {path}"})
        return respond(404, {"error": f"No route for {method} {path}"})
    except HttpError as err:
        return respond(err.status, {"error": str(err)})
    except (ValueError, TypeError) as err:
        return respond(400, {"error": f"Bad input: {err}"})
    except Exception:
        log.exception("Unhandled error")
        return respond(500, {"error": "Internal server error"})