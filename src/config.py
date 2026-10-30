"""Settings read from the environment, plus fixed constants."""

import os

DSQL_ENDPOINT = os.environ.get("DSQL_ENDPOINT", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")

# DSQL closes connections after 1 hour; recycle well before that.
MAX_CONN_AGE_SECONDS = 50 * 60
SERIALIZATION_FAILURE = "40001"  # DSQL optimistic-concurrency conflict -> retry
UNIQUE_VIOLATION = "23505"
