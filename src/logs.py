"""Structured logging: one JSON object per line, so CloudWatch can filter and count by field.

Never log request bodies, emails or tokens. Pass only ids, route patterns and counts.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger()
log.setLevel(logging.INFO)


def log_event(event: str, **fields) -> None:
    """Write {"event": event, ...fields} as one INFO line."""
    log.info(json.dumps({"event": event, **fields}, default=str))
