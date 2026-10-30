"""Reference data shared by everyone: muscle groups and exercises."""

from ..db import query
from ..responses import respond
from ..rows import assert_exists
from ..validation import optional, require_fields, to_float, to_int, to_text


def list_muscle_groups(req):
    return respond(200, query("SELECT id, name FROM workout.muscle_group ORDER BY name"))


def list_exercises(req):
    mg = req["query"].get("muscle_group_id")
    if mg:
        rows = query(
            "SELECT * FROM workout.exercise WHERE muscle_group_id = :mg ORDER BY name",
            mg=to_int(mg, "muscle_group_id"),
        )
    else:
        rows = query("SELECT * FROM workout.exercise ORDER BY name")
    return respond(200, rows)


def create_exercise(req):
    body = req["body"]
    require_fields(body, "name", "muscle_group_id")
    mg = to_int(body["muscle_group_id"], "muscle_group_id")
    assert_exists("muscle_group", mg)
    [row] = query(
        """INSERT INTO workout.exercise (name, muscle_group_id, difficulty, complexity)
           VALUES (:name, :mg, :difficulty, :complexity) RETURNING *""",
        name=to_text(body["name"], "name"),
        mg=mg,
        difficulty=optional(to_float, body.get("difficulty"), "difficulty"),
        complexity=optional(to_text, body.get("complexity"), "complexity"),
    )
    return respond(201, row)
