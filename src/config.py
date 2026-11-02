"""Settings read from the environment, plus fixed constants."""

import os

DSQL_ENDPOINT = os.environ.get("DSQL_ENDPOINT", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")

# DSQL closes connections after 1 hour; recycle well before that.
MAX_CONN_AGE_SECONDS = 50 * 60
SERIALIZATION_FAILURE = "40001"  # DSQL optimistic-concurrency conflict -> retry
UNIQUE_VIOLATION = "23505"

# Copying a friend's program writes one row per day, set and subset in a single
# transaction. DSQL limits the rows one transaction may change (3,000 at the time of
# writing, index entries included), so refuse programs well under that.
PROGRAM_COPY_ROW_LIMIT = 1000
