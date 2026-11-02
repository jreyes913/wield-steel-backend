"""Turn client input into typed values, or reject it with a 400."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from .responses import HttpError


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


def to_username(value: Any, field: str = "username") -> str:
    """A username: 3 to 30 letters, digits, dots, underscores or hyphens. Returned lowercase."""
    text = to_text(value, field).lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", text):
        raise HttpError(400, f"{field} must be 3 to 30 letters, numbers, dots, underscores or hyphens")
    return text


def optional(convert: Callable, value: Any, field: str):
    """Convert a value that is allowed to be missing or null."""
    return None if value is None else convert(value, field)


def path_id(req: dict) -> int:
    return int(req["params"]["id"])


def validate_subsets(subsets: Any) -> list[dict]:
    """Check the optional subsets list sent when creating a set."""
    if subsets is None:
        return []
    if not isinstance(subsets, list):
        raise HttpError(400, "subsets must be a list")
    for i, sub in enumerate(subsets):
        if not isinstance(sub, dict) or sub.get("exercise_id") is None or sub.get("position") is None:
            raise HttpError(400, f"subsets[{i}] needs exercise_id and position")
    return subsets
