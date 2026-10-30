# workout-api

Workout Tracker API: a single AWS Lambda (`workout-api`, Python 3.13, x86_64, us-east-2)
behind API Gateway (HTTP API, Cognito auth), backed by Aurora DSQL.
See the docstring at the top of `lambda_function.py` for the endpoint list.

## Layout

- `lambda_function.py` — Lambda entry point (handler: `lambda_function.handler`); re-exports `src.app.handler`
- `src/` — the API package (routes, auth, database, validation)
- `requirements.txt` — pinned dependencies, bundled into the deployment zip
- `deploy.sh` — builds `build/workout-api.zip` and uploads it to Lambda

## Deploy

Requires the AWS CLI with valid credentials (`aws login`) and `zip`.

```bash
./deploy.sh            # build + update the Lambda's code
./deploy.sh --build    # build only
```

Lambda configuration (env var `DSQL_ENDPOINT`, IAM role, API Gateway routes) is managed
in the AWS console and is not changed by `deploy.sh`.
