"""Database connection, queries and transactions.

The connection lives at module level, so it is reused across invocations
while the Lambda instance stays warm. Each new connection gets a fresh IAM
token from the function's execution role — no stored password.
"""

from __future__ import annotations

import ssl
import time
from typing import Any, Callable

import boto3
import pg8000.native as pg

from .config import AWS_REGION, DSQL_ENDPOINT, MAX_CONN_AGE_SECONDS, SERIALIZATION_FAILURE

_dsql = boto3.client("dsql", region_name=AWS_REGION)
_conn: pg.Connection | None = None
_conn_opened_at = 0.0


def _connect() -> pg.Connection:
    token = _dsql.generate_db_connect_admin_auth_token(
        Hostname=DSQL_ENDPOINT, Region=AWS_REGION
    )
    return pg.Connection(
        user="admin",
        password=token,
        host=DSQL_ENDPOINT,
        port=5432,
        database="postgres",
        ssl_context=ssl.create_default_context(),
        timeout=10,
    )


def get_conn() -> pg.Connection:
    global _conn, _conn_opened_at
    if _conn is None or time.time() - _conn_opened_at > MAX_CONN_AGE_SECONDS:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = _connect()
        _conn_opened_at = time.time()
    return _conn


def _reset_conn() -> None:
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _rows_as_dicts(conn: pg.Connection, rows: list | None) -> list[dict]:
    # Statements that return no rows (DELETE, INSERT without RETURNING) give None.
    if not rows:
        return []
    cols = [c["name"] for c in (conn.columns or [])]
    return [dict(zip(cols, row)) for row in rows]


def pg_code(err: Exception) -> str | None:
    """The PostgreSQL error code (SQLSTATE) of a database error, if any."""
    if isinstance(err, pg.DatabaseError) and err.args and isinstance(err.args[0], dict):
        return err.args[0].get("C")
    return None


def with_retry(fn: Callable[[pg.Connection], Any], attempts: int = 3) -> Any:
    """Run fn(conn), retrying on DSQL conflicts and reconnecting on dropped connections."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(get_conn())
        except pg.InterfaceError:
            # Connection went stale (e.g. idle too long) — reconnect and try again.
            _reset_conn()
            if attempt == attempts:
                raise
        except pg.DatabaseError as err:
            if pg_code(err) != SERIALIZATION_FAILURE or attempt == attempts:
                raise


def query(sql: str, **params) -> list[dict]:
    """Run one statement and return rows as dicts. Use :name placeholders."""
    def run(conn: pg.Connection):
        rows = conn.run(sql, **params)
        return _rows_as_dicts(conn, rows)
    return with_retry(run)


class Tx:
    """Gives code inside a transaction the same query() style."""

    def __init__(self, conn: pg.Connection):
        self._conn = conn

    def query(self, sql: str, **params) -> list[dict]:
        rows = self._conn.run(sql, **params)
        return _rows_as_dicts(self._conn, rows)


def transaction(fn: Callable[[Tx], Any]) -> Any:
    """Run several statements as one all-or-nothing transaction (retried on conflict)."""
    def run(conn: pg.Connection):
        conn.run("BEGIN")
        try:
            result = fn(Tx(conn))
            conn.run("COMMIT")
            return result
        except Exception:
            try:
                conn.run("ROLLBACK")
            except Exception:
                pass
            raise
    return with_retry(run)


def runner(db: Tx | None) -> Callable[..., list[dict]]:
    """Use the transaction if one is given, otherwise a standalone query."""
    return db.query if db else query
