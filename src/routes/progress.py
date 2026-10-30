"""Progress charts: body weight over time and strength for one exercise."""

from ..db import query
from ..responses import HttpError, respond
from ..strength import compute_strength
from ..validation import to_date, to_int


def _date_range_clause(req, column: str, params: dict) -> str:
    """Optional ?start= / ?end= filters, added only when given."""
    clause = ""
    q = req["query"]
    if q.get("start"):
        params["start"] = to_date(q["start"], "start")
        clause += f" AND {column} >= :start"
    if q.get("end"):
        params["end"] = to_date(q["end"], "end")
        clause += f" AND {column} <= :end"
    return clause


def progress_body_weight(req):
    params = {"me": req["athlete_id"]}
    where = _date_range_clause(req, "date", params)
    rows = query(
        f"""SELECT date, body_weight FROM workout.lift
             WHERE athlete_id = :me AND body_weight IS NOT NULL{where}
             ORDER BY date, id""",
        **params,
    )
    return respond(200, rows)


def progress_strength(req):
    q = req["query"]
    if not q.get("exercise_id"):
        raise HttpError(400, "exercise_id is required")
    exercise_id = to_int(q["exercise_id"], "exercise_id")
    exercise = query("SELECT id, name FROM workout.exercise WHERE id = :id", id=exercise_id)
    if not exercise:
        raise HttpError(404, "Exercise not found")

    params = {"me": req["athlete_id"], "ex": exercise_id}
    where = _date_range_clause(req, "l.date", params)
    rows = query(
        f"""SELECT l.id AS lift_id, l.date,
                   s.position AS set_position, sub.position AS subset_position,
                   sub.weight, sub.reps, sub.rir
              FROM workout.lift l
              JOIN workout.exercise_set s ON s.lift_id = l.id
              JOIN workout.exercise_subset sub ON sub.exercise_set_id = s.id
             WHERE l.athlete_id = :me AND sub.exercise_id = :ex
               AND sub.weight IS NOT NULL AND sub.reps IS NOT NULL{where}
             ORDER BY l.date, l.id, s.position, s.id, sub.position, sub.id""",
        **params,
    )
    sets, days = compute_strength(rows)
    return respond(200, {"exercise": exercise[0], "days": days, "sets": sets})
