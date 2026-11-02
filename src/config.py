"""Settings read from the environment, plus fixed constants."""

import os

DSQL_ENDPOINT = os.environ.get("DSQL_ENDPOINT", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")

# DSQL closes connections after 1 hour; recycle well before that.
MAX_CONN_AGE_SECONDS = 50 * 60
SERIALIZATION_FAILURE = "40001"  # DSQL optimistic-concurrency conflict -> retry
UNIQUE_VIOLATION = "23505"
UNDEFINED_TABLE = "42P01"

# Copying a friend's program writes one row per day, set and subset in a single
# transaction. DSQL limits the rows one transaction may change (3,000 at the time of
# writing, index entries included), so refuse programs well under that.
PROGRAM_COPY_ROW_LIMIT = 1000

# API Gateway cuts an HTTP API request off after 30 seconds. DELETE /me stops after this
# many and asks the caller to repeat it, so a big account never hits that wall mid-batch.
DELETE_TIME_BUDGET_SECONDS = 20
# Friendship rows deleted per transaction (each row also changes index entries).
DELETE_FRIENDSHIP_BATCH = 200

# At most this many attempts per athlete, per kind of action, in this many minutes (429 after).
RATE_LIMIT_MAX = 20
RATE_LIMIT_WINDOW_MINUTES = 60
