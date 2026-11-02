"""Lambda entry point: auth check, routing, rate limits, error handling and request logging."""

from __future__ import annotations

import logging
import time

from . import throttle
from .auth import claims_from_event, current_athlete_id
from .logs import log_event
from .responses import HttpError, parse_body, respond
from .routes import ROUTES

log = logging.getLogger()


def _dispatch(event: dict, method: str, path: str, seen: dict) -> dict:
    """Route one request. `seen` collects the route pattern and athlete id for the log line."""
    try:
        claims = claims_from_event(event)  # 401 if not signed in
        path_matched = False
        for route_method, regex, fn, pattern in ROUTES:
            match = regex.match(path)
            if not match:
                continue
            path_matched = True
            if route_method == method:
                seen["route"] = pattern
                athlete_id = current_athlete_id(claims)
                seen["athlete_id"] = athlete_id
                body = parse_body(event)
                action = throttle.action_for(method, pattern, body)
                if action:
                    throttle.check(athlete_id, action)  # 429 when over the limit
                return fn({
                    "athlete_id": athlete_id,
                    "claims": claims,
                    "params": match.groupdict(),
                    "query": event.get("queryStringParameters") or {},
                    "body": body,
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


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")

    # Browser CORS preflight. Browsers never attach the token to it, so it must
    # be answered before the sign-in check. API Gateway adds the CORS
    # headers itself from the API's CORS settings.
    if method == "OPTIONS":
        return {"statusCode": 204}

    started = time.monotonic()
    seen: dict = {}
    response = _dispatch(event, method, path, seen)
    # One line per request. Only the route pattern (never the raw path with ids), the
    # status and the athlete id: no bodies, emails or tokens.
    log_event(
        "request",
        method=method,
        route=seen.get("route", "unmatched"),
        status=response["statusCode"],
        athlete_id=seen.get("athlete_id"),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return response
