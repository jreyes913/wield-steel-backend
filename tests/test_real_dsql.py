"""The same handler and routes against a real Aurora DSQL cluster, in a scratch schema.

Skipped unless both are set:
    DSQL_TEST_ENDPOINT   a cluster endpoint that is NOT production (use staging)
    DSQL_TEST_SCHEMA     a schema name for these tests, never "workout"
Credentials come from the AWS CLI (`aws login`). The schema and its tables are created on the
first run from tests/dsql_schema.sql and every test starts from empty tables, so nothing outside
that schema is touched. This is what covers DSQL's own behaviour: its SQL dialect, identity
columns, timestamptz and uuid defaults, optimistic-concurrency retries and index syntax.
"""

import json
import os
import re
import ssl
import subprocess
from pathlib import Path

import pg8000.native as pg
import pytest

from src import config, db
from src.config import RATE_LIMIT_MAX

from .conftest import add_lift, add_program, befriend

ENDPOINT = os.environ.get("DSQL_TEST_ENDPOINT", "")
SCHEMA = os.environ.get("DSQL_TEST_SCHEMA", "")

pytestmark = pytest.mark.skipif(
    not (ENDPOINT and SCHEMA), reason="set DSQL_TEST_ENDPOINT and DSQL_TEST_SCHEMA to run against DSQL"
)

# Children before parents, so clearing a table never trips over what points at it.
TABLES = [
    "program_exercise_subset", "program_exercise_set", "program_lift", "program",
    "exercise_subset", "exercise_set", "lift", "friendship", "coaching", "rate_limit",
    "athlete", "exercise", "muscle_group",
]


@pytest.fixture(scope="module")
def conn():
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", SCHEMA) or SCHEMA == "workout":
        pytest.exit(f"DSQL_TEST_SCHEMA must be a plain scratch name, not {SCHEMA!r}", returncode=2)
    if ENDPOINT == os.environ.get("DSQL_ENDPOINT"):
        pytest.exit("DSQL_TEST_ENDPOINT is the production endpoint", returncode=2)
    # conftest sets dummy AWS keys so imports work offline; the CLI needs the real login.
    env = {k: v for k, v in os.environ.items() if v != "test" or not k.startswith("AWS_")}
    token = subprocess.run(
        ["aws", "dsql", "generate-db-connect-admin-auth-token", "--hostname", ENDPOINT,
         "--region", "us-east-2"],
        capture_output=True, text=True, check=True, env=env,
    ).stdout.strip()
    c = pg.Connection(user="admin", password=token, host=ENDPOINT, port=5432, database="postgres",
                      ssl_context=ssl.create_default_context(), timeout=30)
    c.run(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    ddl = (Path(__file__).parent / "dsql_schema.sql").read_text().replace("workout.", f"{SCHEMA}.")
    for stmt in re.split(r";\n", ddl):
        stmt = "\n".join(l for l in stmt.splitlines() if not l.startswith("--")).strip()
        if stmt:
            c.run(stmt)
    # Columns added after the first version of the schema (DSQL cannot ADD COLUMN IF NOT EXISTS).
    have = {r[0] for r in c.run(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = 'program'",
        s=SCHEMA)}
    for column, kind in (("is_public", "boolean"), ("assigned_by", "bigint")):
        if column not in have:
            c.run(f"ALTER TABLE {SCHEMA}.program ADD COLUMN {column} {kind}")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def real_db(fake_db, conn, monkeypatch):
    """Send the handler's queries to DSQL (the SQLite stand-in is switched off)."""
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    monkeypatch.setattr(db, "DB_SCHEMA", SCHEMA)
    for t in TABLES:
        conn.run(f"DELETE FROM {SCHEMA}.{t}")
    conn.run(f"INSERT INTO {SCHEMA}.muscle_group (id, name) OVERRIDING SYSTEM VALUE VALUES (1, 'Chest')")
    conn.run(f"INSERT INTO {SCHEMA}.exercise (id, name, muscle_group_id) OVERRIDING SYSTEM VALUE "
             "VALUES (1, 'Bench Press', 1)")


def count(conn, table, where=""):
    return conn.run(f"SELECT COUNT(*) FROM {SCHEMA}.{table} {where}")[0][0]


def test_profile_is_created_once(make_user):
    me = make_user("alice")
    assert me.get("/me")[1]["id"] == me.id
    assert me.get("/me")[1]["username"] == "alice"


def test_lift_round_trip_and_export(make_user):
    me = make_user("alice")
    lift_id = add_lift(me)
    code, lift = me.get(f"/lifts/{lift_id}")
    assert code == 200 and lift["body_weight"] == 180
    assert lift["sets"][0]["subsets"][0]["weight"] == 225
    code, export = me.get("/me/export")
    assert code == 200 and len(export["lifts"]) == 1
    assert "cognito_sub" not in json.dumps(export)


def test_friend_sees_lifts_but_a_stranger_does_not(make_user):
    owner, friend, stranger = make_user("owner"), make_user("friend"), make_user("stranger")
    lift_id = add_lift(owner)
    befriend(owner, friend)
    code, lift = friend.get(f"/athletes/{owner.id}/lifts/{lift_id}")
    assert code == 200 and "body_weight" not in lift
    assert stranger.get(f"/athletes/{owner.id}/lifts/{lift_id}")[0] == 404


def test_rate_limit_counts_and_blocks(make_user):
    me = make_user("alice")
    for _ in range(RATE_LIMIT_MAX):
        assert me.post("/friend-requests", {"username": "nobody"})[0] == 404
    assert me.post("/friend-requests", {"username": "nobody"})[0] == 429


def test_program_copy_is_one_transaction(make_user, conn):
    owner, me = make_user("owner"), make_user("copier")
    pid = add_program(owner, sets=3)
    befriend(owner, me)
    code, copy = me.post(f"/athletes/{owner.id}/programs/{pid}/copy")
    assert code == 201 and [len(d["sets"]) for d in copy["days"]] == [3]
    assert count(conn, "program") == 2


def test_delete_account_removes_every_row(make_user, conn):
    me, other = make_user("alice"), make_user("bobby")
    add_lift(me)
    add_program(me, sets=2)
    befriend(me, other)
    assert me.delete("/me", {"confirm": "alice"}) == (200, {"done": True})
    for table in ("lift", "exercise_set", "exercise_subset", "program", "program_lift",
                  "program_exercise_set", "program_exercise_subset", "friendship", "rate_limit"):
        assert count(conn, table) == 0, table
    assert count(conn, "athlete") == 1  # only bobby is left
    assert other.get("/friends")[1] == []


def test_username_is_unique(make_user):
    a, b = make_user("alice"), make_user("bobby")
    code, _ = b.put("/me", {"username": "alice"})
    assert code == 409


def test_coaching_access_and_cleanup(make_user, conn):
    coach, athlete = make_user("coach"), make_user("athlete")
    lift_id = add_lift(athlete)
    assert coach.post("/coaching-requests", {"username": "athlete"})[0] == 201
    assert coach.get(f"/coached/{athlete.id}/lifts")[0] == 404
    assert athlete.put(f"/coaching-requests/{coach.id}", {"status": "accepted"})[0] == 200
    code, lift = coach.get(f"/coached/{athlete.id}/lifts/{lift_id}")
    assert code == 200 and "body_weight" not in lift
    assert athlete.put(f"/coaches/{coach.id}", {"can_see_body_weight": True})[0] == 200
    assert coach.get(f"/coached/{athlete.id}/lifts")[1][0]["body_weight"] == 180
    assert athlete.delete("/me", {"confirm": "athlete"}) == (200, {"done": True})
    assert count(conn, "coaching") == 0


def test_private_by_default_public_on_request_and_send(make_user, conn):
    coach, a1, a2 = make_user("coach"), make_user("one"), make_user("two")
    for a in (a1, a2):
        assert coach.post("/coaching-requests", {"username": a.username})[0] == 201
        assert a.put(f"/coaching-requests/{coach.id}", {"status": "accepted"})[0] == 200
    pid = add_program(coach, sets=2, public=False)
    assert a1.get(f"/coached/{coach.id}/programs")[0] == 404  # athletes don't read their coach's
    assert coach.get("/programs")[1][0]["is_public"] is False
    code, body = coach.post(f"/programs/{pid}/send", {"athlete_ids": [a1.id, a2.id]})
    assert code == 200 and all(r["ok"] for r in body["results"])
    for a in (a1, a2):
        [got] = a.get("/programs")[1]
        assert got["is_public"] is False and got["assigned_by_name"] == "coach"
        assert [len(d["sets"]) for d in a.get(f"/programs/{got['id']}")[1]["days"]] == [2]
    assert count(conn, "program") == 3
    assert coach.put(f"/programs/{pid}", {"is_public": True})[1]["is_public"] is True
