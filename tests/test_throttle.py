"""Per-athlete limits on abusable routes, and the request log line."""

import json
import logging

from src.config import RATE_LIMIT_MAX


def test_friend_requests_are_limited_per_athlete(make_user):
    a, b = make_user("alice"), make_user("bobby")
    for _ in range(RATE_LIMIT_MAX):
        assert a.post("/friend-requests", {"username": "nobody"})[0] == 404  # failures count too
    code, body = a.post("/friend-requests", {"username": "bobby"})
    assert code == 429 and "Too many" in body["error"]
    assert b.post("/friend-requests", {"username": "nobody"})[0] == 404  # others are unaffected


def test_each_action_has_its_own_counter(make_user):
    a = make_user("alice")
    for _ in range(RATE_LIMIT_MAX):
        a.post("/blocks", {"username": "nobody"})
    assert a.post("/blocks", {"username": "nobody"})[0] == 429
    assert a.post("/friend-requests", {"username": "nobody"})[0] == 404
    assert a.post("/exercises", {"name": "Curl", "muscle_group_id": 1})[0] == 201


def test_put_me_only_counts_username_changes(make_user):
    a = make_user("alice")
    for _ in range(RATE_LIMIT_MAX + 5):
        assert a.put("/me", {"height": 70})[0] == 200
    for i in range(RATE_LIMIT_MAX):
        assert a.put("/me", {"username": f"alice{i}"})[0] == 200
    assert a.put("/me", {"username": "again"})[0] == 429


def test_old_attempts_expire_and_are_deleted(make_user, sql):
    a = make_user("alice")
    for i in range(RATE_LIMIT_MAX):
        sql("INSERT INTO workout.rate_limit (athlete_id, action, created_at) VALUES (:me, 'block', '2000-01-01T00:00:00')", me=a.id)
    assert a.post("/blocks", {"username": "nobody"})[0] == 404  # old rows don't count
    assert sql("SELECT COUNT(*) FROM workout.rate_limit WHERE athlete_id = :me", me=a.id)[0][0] == 1


def test_request_log_has_route_status_and_no_secrets(make_user, caplog):
    a = make_user("alice")
    with caplog.at_level(logging.INFO):
        a.put("/lifts/123", {"date": "2026-01-01"})
    lines = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    entry = [l for l in lines if l["event"] == "request"][-1]
    assert entry["route"] == "/lifts/{id}" and entry["status"] == 404 and entry["athlete_id"] == a.id
    assert set(entry) == {"event", "method", "route", "status", "athlete_id", "duration_ms"}
