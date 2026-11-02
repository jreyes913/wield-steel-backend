"""Usernames: validation, lowercase storage, case-insensitive uniqueness, 409."""

import pytest


@pytest.mark.parametrize("bad", ["ab", "a" * 31, "has space", "semi;colon", "émile", "", "   ", 5])
def test_invalid_usernames_are_400(make_user, bad):
    a = make_user("alice")
    assert a.put("/me", {"username": bad})[0] == 400
    assert a.get("/me")[1]["username"] == "alice"


@pytest.mark.parametrize("good", ["abc", "a" * 30, "a.b_c-d9"])
def test_valid_usernames(make_user, good):
    a = make_user("alice")
    assert a.put("/me", {"username": good})[1]["username"] == good


def test_stored_lowercase(make_user):
    a = make_user("alice")
    assert a.put("/me", {"username": "  MixedCase_1 "})[1]["username"] == "mixedcase_1"


def test_unique_case_insensitively(make_user):
    a, b = make_user("alice"), make_user("bobby")
    assert a.put("/me", {"username": "BOBBY"}) == (409, {"error": "That username is taken"})
    assert b.get("/me")[1]["username"] == "bobby"


def test_keeping_your_own_username_is_fine(make_user):
    a = make_user("alice")
    assert a.put("/me", {"username": "ALICE"})[0] == 200


def test_username_with_other_fields_in_one_request(make_user):
    a = make_user("alice")
    me = a.put("/me", {"username": "alicia", "first_name": "Alice"})[1]
    assert (me["username"], me["first_name"]) == ("alicia", "Alice")


def test_profile_never_returns_cognito_sub(make_user):
    a = make_user("alice")
    assert "cognito_sub" not in a.get("/me")[1]
    assert "cognito_sub" not in a.put("/me", {"height": 70})[1]
