"""Generic row helpers shared by the route handlers."""

from __future__ import annotations

from typing import Any

from .db import Tx, runner
from .responses import HttpError


def assert_exists(table: str, row_id: Any, db: Tx | None = None) -> None:
    """DSQL has no foreign keys, so check referenced rows before writing.

    `table` only ever comes from code in this package, never from user input.
    """
    rows = runner(db)(f"SELECT 1 AS ok FROM workout.{table} WHERE id = :id", id=row_id)
    if not rows:
        raise HttpError(400, f"{table} {row_id} does not exist")


def update_row(table: str, row_id: int, body: dict, fields: dict, db: Tx | None = None) -> dict:
    """Update only the fields present in the body, then return the row.

    fields maps column name -> (converter, required). A required column can
    be changed but not cleared. Column names come from the fields dicts in
    this package, never from the client.
    """
    updates = {}
    for column, (convert, required) in fields.items():
        if column not in body:
            continue
        value = body[column]
        if value is None:
            if required:
                raise HttpError(400, f"{column} cannot be empty")
            updates[column] = None
        else:
            updates[column] = convert(value, column)
    if not updates:
        raise HttpError(400, f"Send at least one of: {', '.join(fields)}")

    set_clause = ", ".join(f"{col} = :{col}" for col in updates)
    rows = runner(db)(
        f"UPDATE workout.{table} SET {set_clause} WHERE id = :row_id RETURNING *",
        row_id=row_id,
        **updates,
    )
    return rows[0]
