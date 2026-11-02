# wield-steel-backend

Workout Tracker API: a single AWS Lambda (`wield-steel-backend`, Python 3.13, x86_64, us-east-2)
behind API Gateway (HTTP API, Cognito auth), backed by Aurora DSQL.
See the docstring at the top of `lambda_function.py` for the endpoint list.

## Layout

- `lambda_function.py` — Lambda entry point (handler: `lambda_function.handler`); re-exports `src.app.handler`
- `src/` — the API package (routes, auth, database, validation)
- `requirements.txt` — pinned dependencies, bundled into the deployment zip
- `requirements-dev.txt` — test-only dependencies (`pytest`); never bundled
- `tests/` — pytest suite
- `deploy.sh` — builds `build/wield-steel-backend.zip` and uploads it to Lambda

## Deploy

Requires the AWS CLI with valid credentials (`aws login`) and `zip`.

```bash
./deploy.sh            # build + update the Lambda's code
./deploy.sh --build    # build only
```

Friends need the `workout.friendship` table, created by hand in the AWS console:
`requester_id`, `addressee_id`, `status` (`pending`, `accepted`, `declined` or `blocked`; for
`blocked`, the requester is the blocker), `created_at` (when it was sent), `updated_at` (last
status change, so when the friendship began), and nullable `requester_hides_lifts` /
`addressee_hides_lifts` booleans. Primary key `(requester_id, addressee_id)`, plus an index on
`addressee_id`. To tidy up once, backfill old rows with `UPDATE workout.friendship SET
created_at = updated_at WHERE created_at IS NULL` and set the two hide flags to `false` where
`NULL` (run in small batches); the code treats `NULL` the same as the backfilled values. Friends read each other's programs and lifts through
`src/routes/friends.py`; those queries list their columns explicitly and never return
`body_weight`, DOB, gender, height or `cognito_sub`.

## Account export and deletion

`GET /me/export` returns everything the caller owns; `DELETE /me {confirm: "<username>"}` deletes
their lifts, programs, friendships (including blocks they made or that name them), profile and
Cognito user (`src/routes/account.py`). Deleting runs in small transactions (one lift, one program,
or up to 200 friendship rows each) because DSQL limits the rows one transaction can change, and the
athlete row goes last so a stopped run can be repeated. After 20 seconds it answers `202
{done: false}`; send the same request again until it answers `200 {done: true}`. If the database
part succeeds but Cognito fails, it answers 502 and says the data is already gone.

Needs, set by the owner: env var `COGNITO_USER_POOL_ID`, and `cognito-idp:AdminDeleteUser` on the
pool for the Lambda's role.

Known gap: an access token stays valid (up to an hour) after its Cognito user is deleted, and
`current_athlete_id()` would create a fresh empty athlete for it; another warm Lambda container may
also still have the old id cached. The frontend stops calling the API after a delete, so this only
matters for a stolen or leftover token.

Lambda configuration (env vars `DSQL_ENDPOINT` and `COGNITO_USER_POOL_ID`, IAM role, API Gateway
routes) is managed in the AWS console and is not changed by `deploy.sh`.
Note that `aws lambda update-function-configuration --environment` replaces the whole variable
map, so always include both variables.

## Limits, logging and monitoring

**Rate limit table (owner runs this first).** Without it the limits are skipped (a
`rate_limit_table_missing` line is logged):

```sql
CREATE TABLE workout.rate_limit (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  athlete_id bigint NOT NULL,
  action     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ASYNC ON workout.rate_limit (athlete_id, action, created_at);
```

Check `workout.athlete.id`'s type first and match `athlete_id` to it. Old rows are deleted
whenever that athlete's counter is read; DSQL has no TTL. Limits are in `src/config.py`
(`RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW_MINUTES`).

**Logs.** Each request writes `{"event":"request","method","route","status","athlete_id",
"duration_ms"}` (route is the pattern, such as `/lifts/{id}`). A failed database connection
writes `{"event":"dsql_connect_failed","error":"<ExceptionName>"}`. For the CloudWatch metric
filter on the Lambda's log group use the pattern `{ $.event = "dsql_connect_failed" }`, and for
server errors `{ $.event = "request" && $.status >= 500 }`.

**Staging.** `deploy.sh` reads `FUNCTION_NAME` and `AWS_REGION`, so a staging Lambda is deployed with
`FUNCTION_NAME=wield-steel-backend-staging ./deploy.sh`. Staging needs its own DSQL cluster
(`DSQL_ENDPOINT`), Cognito pool (`COGNITO_USER_POOL_ID`) and HTTP API pointing at that Lambda; a
second stage on the production API would still call the production Lambda. Never copy real user
data into it.

## Running the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

The tests call the real Lambda handler against an in-memory SQLite database that stands in for
DSQL (`tests/conftest.py` replaces only `get_conn()`), so they need no AWS access and never touch
the `workout` schema. They cover friend access and privacy, requests and blocks, hiding lifts,
usernames, program copy (including the 422 limit) and account export/deletion. SQLite accepts the
plain SQL this API uses but is not DSQL: anything that depends on DSQL's own behaviour (row
limits per transaction, query forms it rejects, real unique-violation errors) is not covered, and
there are no tests against a scratch DSQL schema yet. `requirements-dev.txt` is not read by
`deploy.sh`, so the Lambda package is unchanged.
