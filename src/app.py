"""Lambda entry point: auth check, routing and error handling."""

from __future__ import annotations

import logging

from .auth import claims_from_event, current_athlete_id
from .responses import HttpError, parse_body, respond
from .routes import ROUTES

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")

    # Browser CORS preflight. Browsers never attach the token to it, so it must
    # be answered before the sign-in check below. API Gateway adds the CORS
    # headers itself from the API's CORS settings.
    if method == "OPTIONS":
        return {"statusCode": 204}

    try:
        claims = claims_from_event(event)  # 401 if not signed in
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
                    "body": parse_body(event),
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
