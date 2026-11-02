# wield-steel-backend

Workout Tracker API: a single AWS Lambda (`wield-steel-backend`, Python 3.13, x86_64, us-east-2)
behind API Gateway (HTTP API, Cognito auth), backed by Aurora DSQL.
See the docstring at the top of `lambda_function.py` for the endpoint list.

## Layout

- `lambda_function.py` — Lambda entry point (handler: `lambda_function.handler`); re-exports `src.app.handler`
- `src/` — the API package (routes, auth, database, validation)
- `requirements.txt` — pinned dependencies, bundled into the deployment zip
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

Lambda configuration (env var `DSQL_ENDPOINT`, IAM role, API Gateway routes) is managed
in the AWS console and is not changed by `deploy.sh`.
