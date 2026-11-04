"""Test setup: runs the real handler against an in-memory SQLite stand-in for DSQL.

SQLite understands the plain SQL this API uses (named :params, RETURNING, JOINs, a
second schema attached as "workout"). Only get_conn() is replaced, so the real
query(), transaction() and every route run unchanged. SQL that only DSQL or
PostgreSQL accepts is not covered here (see "Running the tests" in the README).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime

import pytest
from botocore.exceptions import ClientError

# boto3 clients are created when src is imported; dummy credentials keep that offline.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")

from src import auth, db  # noqa: E402
from src.app import handler  # noqa: E402

sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_adapter(datetime, lambda d: d.isoformat())

SCHEMA = """
CREATE TABLE workout.athlete (id INTEGER PRIMARY KEY, cognito_sub TEXT UNIQUE, username TEXT UNIQUE,
  first_name TEXT, last_name TEXT, dob TEXT, gender TEXT, height INTEGER);
CREATE TABLE workout.muscle_group (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE workout.exercise (id INTEGER PRIMARY KEY, name TEXT, muscle_group_id INTEGER,
  difficulty REAL, complexity TEXT);
CREATE TABLE workout.lift (id INTEGER PRIMARY KEY, date TEXT, athlete_id INTEGER, body_weight INTEGER);
CREATE TABLE workout.exercise_set (id INTEGER PRIMARY KEY, lift_id INTEGER, position INTEGER);
CREATE TABLE workout.exercise_subset (id INTEGER PRIMARY KEY, exercise_set_id INTEGER,
  exercise_id INTEGER, position INTEGER, reps INTEGER, rir INTEGER, weight REAL);
CREATE TABLE workout.program (id INTEGER PRIMARY KEY, athlete_id INTEGER, name TEXT,
  start_date TEXT, end_date TEXT);
CREATE TABLE workout.program_lift (id INTEGER PRIMARY KEY, program_id INTEGER, name TEXT);
CREATE TABLE workout.program_exercise_set (id INTEGER PRIMARY KEY, program_lift_id INTEGER, position INTEGER);
CREATE TABLE workout.program_exercise_subset (id INTEGER PRIMARY KEY, program_exercise_set_id INTEGER,
  exercise_id INTEGER, position INTEGER, reps INTEGER);
CREATE TABLE workout.friendship (requester_id INTEGER, addressee_id INTEGER, status TEXT,
  created_at TEXT, updated_at TEXT, requester_hides_lifts INTEGER, addressee_hides_lifts INTEGER,
  PRIMARY KEY (requester_id, addressee_id));
CREATE TABLE workout.rate_limit (id INTEGER PRIMARY KEY, athlete_id INTEGER, action TEXT, created_at TEXT);
INSERT INTO workout.muscle_group (id, name) VALUES (1, 'Chest');
INSERT INTO workout.exercise (id, name, muscle_group_id) VALUES (1, 'Bench Press', 1);
"""


class FakeConn:
    """The few bits of a pg8000 connection that src/db.py uses."""

    def __init__(self):
        self._db = sqlite3.connect(":memory:", isolation_level=None)
        self._db.create_function("now", 0, lambda: datetime.now().isoformat())
        self._db.execute("ATTACH DATABASE ':memory:' AS workout")
        self._db.executescript(SCHEMA)
        self.columns: list[dict] = []

    def run(self, sql, **params):
        cur = self._db.execute(sql, params)
        self.columns = [{"name": d[0]} for d in cur.description or []]
        return cur.fetchall() if cur.description else None

    def close(self):
        pass


class FakeCognito:
    """Records admin_delete_user calls; set .error to make it fail."""

    def __init__(self):
        self.deleted: list[str] = []
        self.error: Exception | None = None
        self.gone: set[str] = set()  # usernames Cognito no longer knows

    def admin_get_user(self, UserPoolId, Username):
        if Username in self.gone:
            raise ClientError({"Error": {"Code": "UserNotFoundException"}}, "AdminGetUser")
        return {"Username": Username}

    def admin_delete_user(self, UserPoolId, Username):
        if self.error:
            raise self.error
        self.deleted.append(Username)


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(auth, "_athlete_cache", {})
    cognito = FakeCognito()
    monkeypatch.setattr("src.routes.account._cognito", cognito)
    monkeypatch.setattr(auth, "_cognito", cognito)
    monkeypatch.setattr(auth, "COGNITO_USER_POOL_ID", "pool")
    conn.cognito = cognito
    return conn


@pytest.fixture
def sql(fake_db):
    """Run raw SQL against the fake database (for setting up and checking rows)."""
    return fake_db.run


class Caller:
    """One signed-in user making API calls through the real handler."""

    def __init__(self, sub: str, username: str):
        self.sub = sub
        self.username = username
        self.id = self.call("GET", "/me")[1]["id"]  # first call creates the athlete

    def call(self, method, path, body=None, query=None):
        event = {
            "rawPath": path,
            "requestContext": {
                "http": {"method": method},
                "authorizer": {"jwt": {"claims": {"sub": self.sub, "username": self.username}}},
            },
            "queryStringParameters": query,
            "body": None if body is None else json.dumps(body),
        }
        out = handler(event, None)
        return out["statusCode"], json.loads(out["body"]) if out.get("body") else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None):
        return self.call("POST", path, body)

    def put(self, path, body=None):
        return self.call("PUT", path, body)

    def delete(self, path, body=None):
        return self.call("DELETE", path, body)


@pytest.fixture
def make_user():
    count = 0

    def make(username: str) -> Caller:
        nonlocal count
        count += 1
        return Caller(f"sub-{count}", username)

    return make


def befriend(a: Caller, b: Caller) -> None:
    assert a.post("/friend-requests", {"username": b.username})[0] == 201
    assert b.put(f"/friend-requests/{a.id}", {"status": "accepted"})[0] == 200


def add_lift(user: Caller, with_body_weight=True) -> int:
    """A lift with one set holding one subset."""
    code, lift = user.post("/lifts", {"date": "2026-01-05", "body_weight": 180 if with_body_weight else None})
    assert code == 201
    code, s = user.post(f"/lifts/{lift['id']}/sets", {
        "position": 1, "subsets": [{"exercise_id": 1, "position": 1, "reps": 5, "rir": 2, "weight": 225}],
    })
    assert code == 201, s
    return lift["id"]


def add_program(user: Caller, sets: int = 1) -> int:
    """A program with one day holding `sets` sets of one subset each."""
    code, p = user.post("/programs", {"name": "Push"})
    code, day = user.post(f"/programs/{p['id']}/days", {"name": "Day 1"})
    for i in range(sets):
        code, _ = user.post(f"/program-days/{day['id']}/sets", {
            "position": i + 1, "subsets": [{"exercise_id": 1, "position": 1, "reps": 8}],
        })
        assert code == 201
    return p["id"]
