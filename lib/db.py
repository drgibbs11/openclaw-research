"""Postgres access.

Connection gotcha (build-spec §3): Supabase's transaction pooler (port 6543)
does not support prepared statements, which psycopg3 uses by default after a
few executions. We pass prepare_threshold=None unconditionally — it is correct
on the pooler and costs nothing measurable on a direct connection for
low-connection-count cron jobs.

G3: every write here is idempotent. Upserts use ON CONFLICT; snapshot rows are
keyed (ticker, run_ts).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


# All screener objects live here, never in `public` — the target database is a
# live weather-trading system whose `public` schema already contains an
# unrelated `market_snapshots`. Setting search_path on the connection keeps
# every query in this codebase unqualified while still resolving to our own
# tables. Override only if you deliberately relocated the schema.
SCHEMA = os.environ.get("SCREENER_SCHEMA", "screener")


# A dropped connection must raise, not hang. Job C ran for 3.5 hours and then
# stopped dead mid-run: zero CPU, memory frozen to the byte, no further log
# lines and no exception, for 90 minutes until it was killed. psycopg sets no
# socket timeout by default, so when the pooler drops a connection the client
# blocks forever on a read that will never return (D36).
#
# It stalls silently rather than crashing, which is the worst shape for a cron
# worker: `restartPolicyType: NEVER` means nothing restarts it and Railway
# still reports the deployment as SUCCESS, so a run can be dead for hours and
# look healthy from the outside.
#
# keepalives detect a dead peer in roughly 30 + 5x10 = 80s and surface it as an
# OperationalError, which the job then fails on loudly with exit 1 — the
# behaviour every other failure path here already has.
CONNECT_KWARGS = {
    "connect_timeout": 15,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


@contextmanager
def connect(autocommit: bool = False):
    conn = psycopg.connect(dsn(), prepare_threshold=None, autocommit=autocommit,
                           **CONNECT_KWARGS)
    try:
        with conn.cursor() as cur:
            cur.execute(f"set search_path to {SCHEMA}")
            # Fail loudly rather than silently falling through to public, where
            # an insert would either error on missing columns or, worse, land
            # in somebody else's table.
            cur.execute("select to_regclass(%s)", (f"{SCHEMA}.series",))
            if cur.fetchone()[0] is None:
                raise RuntimeError(
                    f"schema '{SCHEMA}' is missing or not initialised — "
                    "run sql/001_schema.sql first"
                )
        if autocommit is False:
            conn.commit()
    except Exception:
        conn.close()
        raise
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def upsert(conn, table: str, rows: Sequence[dict], conflict: str,
           update_cols: Iterable[str] | None = None) -> int:
    """Bulk INSERT ... ON CONFLICT DO UPDATE. Returns rows written.

    `update_cols` defaults to every non-key column. Pass an empty iterable for
    DO NOTHING semantics.
    """
    rows = [r for r in rows if r]
    if not rows:
        return 0

    cols = list(rows[0].keys())
    keys = [k.strip() for k in conflict.split(",")]
    updates = list(update_cols) if update_cols is not None else [c for c in cols if c not in keys]

    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    sql = (
        f"insert into {table} ({', '.join(cols)}) values {placeholders} "
        f"on conflict ({conflict}) "
    )
    sql += (
        "do nothing"
        if not updates
        else "do update set " + ", ".join(f"{c} = excluded.{c}" for c in updates)
    )

    values = [tuple(r.get(c) for c in cols) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(sql, values)
    return len(values)


def get_job_cursor(conn, job_name: str):
    with conn.cursor() as cur:
        cur.execute("select cursor_ts, cursor_text from job_state where job_name = %s", (job_name,))
        row = cur.fetchone()
    return row if row else (None, None)


def set_job_cursor(conn, job_name: str, cursor_ts=None, cursor_text=None, notes: dict | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into job_state (job_name, cursor_ts, cursor_text, updated_at, notes)
            values (%s, %s, %s, now(), %s)
            on conflict (job_name) do update set
              cursor_ts = excluded.cursor_ts,
              cursor_text = excluded.cursor_text,
              updated_at = excluded.updated_at,
              notes = excluded.notes
            """,
            (job_name, cursor_ts, cursor_text, Jsonb(notes) if notes is not None else None),
        )


def apply_sql_file(conn, path: str) -> None:
    with open(path) as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
