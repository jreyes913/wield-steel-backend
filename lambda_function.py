"""Workout Tracker API — one Lambda, many endpoints.

Runs behind API Gateway (HTTP API) and talks to Aurora DSQL.

Environment variables (set in the Lambda console):
    DSQL_ENDPOINT   e.g. abc123.dsql.us-east-2.on.aws
    AWS_REGION      set automatically by Lambda

Every request must come through API Gateway with a valid Cognito login.
The caller's identity is read from the token, never from the request body,
so a user can only ever see or change their own data. Anything that isn't
yours returns 404, so other people's IDs aren't revealed. The one exception is
an accepted friend, who can read (never change) your programs and logged lifts,
and sees only your name and username: never body weight or other profile fields.

Endpoints
---------
Profile
    GET    /me                          your profile (created on first call)
    PUT    /me                          {username?, first_name?, last_name?, dob?, gender?, height?}
                                        username: 3-30 of a-z 0-9 . _ - (stored lowercase), unique, else 409

Reference data (shared by everyone)
    GET    /muscle-groups
    GET    /exercises                   optional ?muscle_group_id=1
    POST   /exercises                   {name, muscle_group_id, difficulty?, complexity?}

Logged workouts
    GET    /lifts                       your lifts, newest first
    POST   /lifts                       {date, body_weight?}
    GET    /lifts/{id}                  one lift with its sets and subsets
    PUT    /lifts/{id}                  {date?, body_weight?}
    DELETE /lifts/{id}                  deletes the lift, its sets and subsets
    POST   /lifts/{id}/sets             {position, subsets: [{exercise_id, position, reps?, rir?, weight?}]}
    POST   /lifts/{id}/apply            {program_day_id}  copy a program day into the lift
    PUT    /sets/{id}                   {position}
    DELETE /sets/{id}                   deletes the set and its subsets
    POST   /sets/{id}/subsets           {exercise_id, position, reps?, rir?, weight?}
    PUT    /subsets/{id}                {exercise_id?, position?, reps?, rir?, weight?}
    DELETE /subsets/{id}

Progress
    GET    /progress/body-weight        optional ?start=YYYY-MM-DD&end=YYYY-MM-DD
    GET    /progress/strength           ?exercise_id=1, optional start/end

Programs (templates). A program has days, days have sets, sets have subsets.
    GET    /programs                    your programs
    POST   /programs                    {name, start_date?, end_date?}
    GET    /programs/{id}               the full tree: days -> sets -> subsets
    PUT    /programs/{id}               {name?, start_date?, end_date?}
    DELETE /programs/{id}               deletes everything under it
    POST   /programs/{id}/days          {name}
    PUT    /program-days/{id}           {name}
    DELETE /program-days/{id}
    POST   /program-days/{id}/sets      {position, subsets: [{exercise_id, position, reps?}]}
    PUT    /program-sets/{id}           {position}
    DELETE /program-sets/{id}
    POST   /program-sets/{id}/subsets   {exercise_id, position, reps?}
    PUT    /program-subsets/{id}        {exercise_id?, position?, reps?}
    DELETE /program-subsets/{id}

Friends. Needs the workout.friendship table (requester_id, addressee_id, status, created_at,
updated_at, requester_hides_lifts, addressee_hides_lifts); status may also be 'blocked'.
A friend's data is read-only; every other athlete's id returns 404.
    GET    /friends                     [{athlete, since, hide_my_lifts}], by name
    PUT    /friends/{id}                {hide_my_lifts: true|false}  hide your lifts from this friend
    DELETE /friends/{id}                remove the friend with athlete id
    GET    /friend-requests             {incoming: [{athlete, sent_at}], outgoing: [...]}
    POST   /friend-requests             {username}  -> {status: "pending"|"accepted", athlete}
    PUT    /friend-requests/{id}        {status: "accepted"|"declined"}  id = the requester's athlete id
    DELETE /friend-requests/{id}        cancel your outgoing request to athlete id
    GET    /athletes/{id}               a friend's public profile (id, username, name)
    GET    /athletes/{id}/lifts         a friend's lifts: id, date
    GET    /athletes/{id}/lifts/{lift_id}      one lift with sets and subsets, no body weight
    GET    /athletes/{id}/programs      a friend's programs
    GET    /athletes/{id}/programs/{program_id}  the full program tree
    POST   /athletes/{id}/programs/{program_id}/copy   copy it into your own programs (201)

Blocking. A block removes any friendship or request, and the blocked athlete just sees
"No athlete with that username".
    GET    /blocks                      [{athlete, since}], newest first
    POST   /blocks                      {username}  block by exact username
    PUT    /blocks/{id}                 block athlete id (no-op if already blocked)
    DELETE /blocks/{id}                 unblock

Notes on Aurora DSQL
--------------------
* No foreign keys: parents are checked before inserting children, and
  deletes remove children first, inside one transaction.
* Queries are kept to simple shapes (plain JOINs, one-row lookups, deletes
  by a single id) because DSQL rejects some PostgreSQL query forms.

Code layout (src/)
------------------
    app.py          handler: auth check, routing, error handling
    routes/         one module per endpoint group, plus the ROUTES table
    auth.py         caller identity, ownership and friendship checks
    db.py           DSQL connection, query(), transaction()
    rows.py         assert_exists(), update_row()
    cascade.py      child-first deletes
    strength.py     1RM / intensity / load / volume math
    validation.py   input converters (to_int, to_date, ...)
    responses.py    HttpError, respond(), parse_body()
    config.py       environment variables and constants

Handler setting in Lambda: lambda_function.handler
"""

from src.app import handler

__all__ = ["handler"]
