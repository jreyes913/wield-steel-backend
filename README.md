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

Needs, set by the owner: env var `COGNITO_USER_POOL_ID`, and `cognito-idp:AdminDeleteUser` + `cognito-idp:AdminGetUser` on the
pool for the Lambda's role.

Lambda configuration (env vars `DSQL_ENDPOINT` and `COGNITO_USER_POOL_ID`, IAM role, API Gateway
routes) is managed in the AWS console and is not changed by `deploy.sh`.
Note that `aws lambda update-function-configuration --environment` replaces the whole variable
map, so always include every variable (`DSQL_ENDPOINT`, `COGNITO_USER_POOL_ID`, `ORIGIN_SECRET`,
`ORIGIN_SECRET_REQUIRED`).

**Deleted accounts and old tokens.** A deleted user's access token stays valid for up to an hour.
Before a new profile is created, `src/auth.py` asks Cognito (`AdminGetUser`) whether the user still
exists and answers 401 if not. The per-container athlete-id cache expires after 60 seconds, so a
container holding the id of a deleted account stops writing for it within a minute.

**Closing the execute-api bypass.** The CloudFront origin sends a secret header
`X-Origin-Verify`; the Lambda compares it with env var `ORIGIN_SECRET` (`src/app.py`). With
`ORIGIN_SECRET_REQUIRED=true` a request without the header gets 403, so calling the
`execute-api` address directly (which skips the WAF) fails. `OPTIONS` preflights are exempt.
To rotate: deploy with the new secret in `ORIGIN_SECRET` and `ORIGIN_SECRET_REQUIRED` unset,
change the CloudFront header, then set `ORIGIN_SECRET_REQUIRED=true` again.

## Coaching

"Coach" is a link between two athletes, not an account type (`src/routes/coaching.py`, table
`workout.coaching`, one row per pair: `coach_id`, `athlete_id`, `status`, `can_see_body_weight`).
The coach asks by username (`POST /coaching-requests`), only the athlete can accept or decline
(`PUT /coaching-requests/{coach id}`), change body-weight sharing (`PUT /coaches/{id}`) or remove
the coach (`DELETE /coaches/{id}`); the coach can also stop (`DELETE /coached/{athlete id}`).
`GET /coaching` lists both sides and pending requests. An accepted coach has read-only routes under
`/coached/{athlete id}` for the profile, lifts and programs, guarded by `require_coach_of()`
(`src/auth.py`); anything else is 404. Body weight is returned only when allowed, and never DOB,
gender, height or `cognito_sub`. A declined request looks pending to the coach, blocking ends a
link and hides the athlete, and coach requests are rate limited. Export lists coaches and athletes,
and account deletion removes the rows. Create the table before deploying (DDL in
`tests/dsql_schema.sql`).

### Program visibility and sending

`workout.program.is_public` (nullable boolean; NULL means private, because DSQL cannot add a
column with a default) controls what friends and coaches see: friend and coach program routes
only return public programs, and copying a friend's program needs it public. A friend's copy is a
clean, independent copy (`src/program_copy.py`), no syncing.

**Programs a coach sends are kept in sync.** `POST /programs/{id}/send` with `{"athlete_ids": [...]}`
(at most 50, rate limited, sending twice is harmless) gives each accepted athlete a program row with
`source_program_id` pointing at the coach's program and `assigned_by` the coach. The row has no days
of its own: reads (`GET /programs`, `GET /programs/{id}`, applying a day to a lift via
`readable_program_day`, export) use the coach's name, dates and days, so every add, delete or
rename shows at once and nothing needs copying. For the athlete it is read-only (409 on edits) and
can never be made public; it can be deleted. The sync ends, by `detach_program()`, when the coach
deletes the program, the coaching link ends (either side removes it, or blocks), or the coach
deletes their account: the athlete's row is filled with a copy of what it showed (day by day, in
separate transactions) and keeps `assigned_by`, so it stays private for good but is now editable.
A received program can't be sent on. If the coach's account is gone, the list shows no sender name.

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
CREATE INDEX ASYNC rate_limit_lookup ON workout.rate_limit (athlete_id, action, created_at);
```

Check `workout.athlete.id`'s type first and match `athlete_id` to it. Old rows are deleted
whenever that athlete's counter is read; DSQL has no TTL. Limits are in `src/config.py`
(`RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW_MINUTES`).

**Logs.** Each request writes `{"event":"request","method","route","status","athlete_id",
"duration_ms"}` (route is the pattern, such as `/lifts/{id}`). A failed database connection
writes `{"event":"dsql_connect_failed","error":"<ExceptionName>"}`. For the CloudWatch metric
filter on the Lambda's log group use the pattern `{ $.event = "dsql_connect_failed" }`, and for
server errors `{ $.event = "request" && $.status >= 500 }`.

**WAF alarm.** An organization policy denies creating alarms in us-east-1, where the CloudFront
WAF reports `BlockedRequests` (reading it is allowed). So the Lambda `wield-steel-waf-metric`
(`monitoring/waf_blocked_requests.py`, run every 5 minutes by the EventBridge rule of the same
name, role `wield-steel-waf-metric-role`) copies the previous 5-minute total into
`WieldSteel/WafBlockedRequests` in us-east-2, and the alarm `waf-blocked-requests` (100 or more
in 5 minutes, a placeholder threshold) emails the alerts topic. Update the code with
`zip f.zip waf_blocked_requests.py` and `aws lambda update-function-code`; `deploy.sh` does not
touch it. It only reads us-east-1 and creates nothing there.

**Staging.** `deploy.sh` reads `FUNCTION_NAME` and `AWS_REGION`, so a staging Lambda is deployed with
`FUNCTION_NAME=wield-steel-backend-staging ./deploy.sh`. Staging needs its own DSQL cluster
(`DSQL_ENDPOINT`), Cognito pool (`COGNITO_USER_POOL_ID`) and HTTP API pointing at that Lambda; a
second stage on the production API would still call the production Lambda. Never copy real user
data into it.

Staging as built (us-east-2, 2026-11-04): DSQL cluster `rvugtya5xyrzoy6uyuixxhewia` (deletion
protection off), Cognito pool `us-east-2_XDQcri7AS` (client `483euju4s3l5fhf6l2gp9i715a`),
Lambda `wield-steel-backend-staging` with its own role, HTTP API `iev40if5y5` (CORS allows
localhost and the Amplify `staging` branch), and the Amplify branch `staging`. The schema was
rebuilt from production and only the shared `muscle_group` and `exercise` rows were copied.
Staging has no `ORIGIN_SECRET`, so its execute-api address is open. Add testers with
`aws cognito-idp admin-create-user` (sign-up is admin-only there too).

## Running the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

The tests call the real Lambda handler against an in-memory SQLite database that stands in for
DSQL (`tests/conftest.py` replaces only `get_conn()`), so they need no AWS access and never touch
the `workout` schema. They cover friend access and privacy, requests and blocks, hiding lifts,
usernames, program copy (including the 422 limit) and account export/deletion. SQLite is not
DSQL, so `tests/test_real_dsql.py` runs the main flows (profile, lifts, export, friend access,
rate limits, program copy, account deletion, unique usernames) against a real cluster. It is
skipped unless both variables are set:

```bash
DSQL_TEST_ENDPOINT=<staging cluster endpoint> DSQL_TEST_SCHEMA=wield_test python -m pytest tests/test_real_dsql.py
```

Use the staging cluster, never production; the run refuses the production endpoint and the name
`workout`. It needs `aws login` credentials (it asks the AWS CLI for a DSQL token), creates the
schema from `tests/dsql_schema.sql` on the first run, and empties its tables before each test.
Keep `tests/dsql_schema.sql` in step with production. Row limits per transaction are still not
tested. `requirements-dev.txt` is not read by
`deploy.sh`, so the Lambda package is unchanged.
