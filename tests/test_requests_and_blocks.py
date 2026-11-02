"""Friend request and block rules (PROMPT_009 section 3, PROMPT_010 sections 3.6 and 9.3)."""

from .conftest import befriend


def rows(sql):
    return sql("SELECT requester_id, addressee_id, status FROM workout.friendship ORDER BY 1, 2")


def test_unknown_and_own_username(make_user):
    a = make_user("alice")
    assert a.post("/friend-requests", {"username": "nobody"}) == (404, {"error": "No athlete with that username"})
    assert a.post("/friend-requests", {"username": "Alice"})[0] == 400


def test_username_match_is_case_insensitive(make_user):
    a, b = make_user("alice"), make_user("bobby")
    assert a.post("/friend-requests", {"username": "BOBBY"})[0] == 201


def test_pending_then_duplicate_is_409(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    code, body = a.post("/friend-requests", {"username": "bobby"})
    assert (code, body["status"]) == (201, "pending")
    assert a.post("/friend-requests", {"username": "bobby"})[0] == 409
    assert len(rows(sql)) == 1


def test_reverse_pending_request_is_accepted_in_one_step(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    a.post("/friend-requests", {"username": "bobby"})
    code, body = b.post("/friend-requests", {"username": "alice"})
    assert body["status"] == "accepted"
    assert rows(sql) == [(a.id, b.id, "accepted")]  # still one row per pair


def test_already_friends_is_409(make_user):
    a, b = make_user("alice"), make_user("bobby")
    befriend(a, b)
    assert a.post("/friend-requests", {"username": "bobby"})[0] == 409
    assert b.post("/friend-requests", {"username": "alice"})[0] == 409


def test_only_addressee_answers_and_only_while_pending(make_user):
    a, b = make_user("alice"), make_user("bobby")
    a.post("/friend-requests", {"username": "bobby"})
    assert a.put(f"/friend-requests/{b.id}", {"status": "accepted"})[0] == 404  # sender can't answer
    assert b.put(f"/friend-requests/{a.id}", {"status": "maybe"})[0] == 400
    assert b.put(f"/friend-requests/{a.id}", {"status": "accepted"})[0] == 200
    assert b.put(f"/friend-requests/{a.id}", {"status": "declined"})[0] == 409


def test_declined_is_private_and_blocks_resending(make_user):
    a, b = make_user("alice"), make_user("bobby")
    a.post("/friend-requests", {"username": "bobby"})
    b.put(f"/friend-requests/{a.id}", {"status": "declined"})
    outgoing = a.get("/friend-requests")[1]["outgoing"]
    assert [o["athlete"]["username"] for o in outgoing] == ["bobby"]  # looks pending
    assert b.get("/friend-requests")[1]["incoming"] == []  # the decliner doesn't see it again
    assert a.post("/friend-requests", {"username": "bobby"}) == (409, {"error": "Request declined"})
    # But the decliner can still send one the other way.
    assert b.post("/friend-requests", {"username": "alice"})[0] == 201


def test_sending_after_you_declined_replaces_their_row(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    a.post("/friend-requests", {"username": "bobby"})
    b.put(f"/friend-requests/{a.id}", {"status": "declined"})
    assert b.post("/friend-requests", {"username": "alice"})[1]["status"] == "pending"
    assert rows(sql) == [(b.id, a.id, "pending")]


def test_cancel_and_remove_delete_the_row(make_user, sql):
    a, b, c = make_user("alice"), make_user("bobby"), make_user("carol")
    a.post("/friend-requests", {"username": "bobby"})
    assert b.delete(f"/friend-requests/{a.id}")[0] == 404  # only the sender can cancel
    assert a.delete(f"/friend-requests/{b.id}")[0] == 200
    befriend(a, c)
    assert c.delete(f"/friends/{a.id}")[0] == 200  # either side can remove
    assert rows(sql) == []


def test_block_removes_relationship_and_hides_itself(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    befriend(a, b)
    assert a.put(f"/blocks/{b.id}")[0] == 200
    assert rows(sql) == [(a.id, b.id, "blocked")]
    # The blocked athlete sees "no such username", and can't see the block anywhere.
    assert b.post("/friend-requests", {"username": "alice"}) == (404, {"error": "No athlete with that username"})
    assert b.get("/friends")[1] == [] and a.get("/friends")[1] == []
    for who in (a, b):
        r = who.get("/friend-requests")[1]
        assert r == {"incoming": [], "outgoing": []}
    assert b.get("/blocks")[1] == []
    # The blocker is told to unblock first.
    assert a.post("/friend-requests", {"username": "bobby"}) == (409, {"error": "Unblock them first"})


def test_block_clears_pending_and_declined_rows_both_directions(make_user, sql):
    a, b, c = make_user("alice"), make_user("bobby"), make_user("carol")
    b.post("/friend-requests", {"username": "alice"})          # pending toward alice
    a.post("/friend-requests", {"username": "carol"})
    c.put(f"/friend-requests/{a.id}", {"status": "declined"})  # declined
    a.put(f"/blocks/{b.id}")
    a.put(f"/blocks/{c.id}")
    assert rows(sql) == [(a.id, b.id, "blocked"), (a.id, c.id, "blocked")]


def test_block_rules(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    assert a.put(f"/blocks/{a.id}")[0] == 400
    assert a.put("/blocks/999999")[0] == 404
    assert a.put(f"/blocks/{b.id}")[0] == 200
    assert a.put(f"/blocks/{b.id}")[0] == 200  # blocking again is a no-op
    assert len(rows(sql)) == 1
    assert [x["athlete"]["username"] for x in a.get("/blocks")[1]] == ["bobby"]
    assert b.delete(f"/blocks/{a.id}")[0] == 404  # only the blocker can unblock
    assert a.delete(f"/blocks/{b.id}")[0] == 200
    assert rows(sql) == []  # back to no relationship, nothing restored
    assert a.delete(f"/blocks/{b.id}")[0] == 404


def test_two_way_block_keeps_both_rows_and_unblock_is_independent(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    a.put(f"/blocks/{b.id}")
    b.put(f"/blocks/{a.id}")  # blocking back must not lift alice's block
    assert rows(sql) == [(a.id, b.id, "blocked"), (b.id, a.id, "blocked")]
    a.delete(f"/blocks/{b.id}")
    assert rows(sql) == [(b.id, a.id, "blocked")]
    # alice is still blocked by bobby: she can't reach him.
    assert a.post("/friend-requests", {"username": "bobby"}) == (404, {"error": "No athlete with that username"})
    assert a.get(f"/athletes/{b.id}")[0] == 404


def test_block_by_username(make_user, sql):
    a, b = make_user("alice"), make_user("bobby")
    assert a.post("/blocks", {"username": "nobody"})[0] == 404
    assert a.post("/blocks", {"username": "ALICE"})[0] == 400
    assert a.post("/blocks", {"username": "Bobby"})[0] == 200
    assert rows(sql) == [(a.id, b.id, "blocked")]
    assert a.post("/blocks", {"username": "bobby"})[0] == 200  # no-op again
    assert len(rows(sql)) == 1
