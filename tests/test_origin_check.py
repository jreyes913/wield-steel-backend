"""The shared-secret check that refuses requests which skipped CloudFront."""

import pytest

from src import config
from src.app import handler


def _event(headers, method="GET"):
    return {
        "rawPath": "/me",
        "headers": headers,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": {"sub": "s-origin", "username": "origin-user"}}},
        },
    }


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(config, "ORIGIN_SECRET", "s3cret")


def test_off_when_no_secret_configured():
    assert handler(_event({}), None)["statusCode"] == 200


def test_wrong_header_is_refused(secret):
    assert handler(_event({"x-origin-verify": "nope"}), None)["statusCode"] == 403


def test_right_header_is_allowed(secret):
    assert handler(_event({"x-origin-verify": "s3cret"}), None)["statusCode"] == 200


def test_missing_header_allowed_until_required(secret, monkeypatch):
    assert handler(_event({}), None)["statusCode"] == 200
    monkeypatch.setattr(config, "ORIGIN_SECRET_REQUIRED", True)
    assert handler(_event({}), None)["statusCode"] == 403


def test_preflight_still_works_when_required(secret, monkeypatch):
    monkeypatch.setattr(config, "ORIGIN_SECRET_REQUIRED", True)
    assert handler(_event({}, method="OPTIONS"), None)["statusCode"] == 204
