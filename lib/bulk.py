"""Batched, change-filtered upserts (2026-10-23 Supabase perf fix).

Why this exists: the snapshot job re-upserted every event (~900k) every 6 hours
with one INSERT ... ON CONFLICT DO UPDATE statement per row (executemany), and
the DO UPDATE fired unconditionally, so every run rewrote ~900k full rows
(including the `raw` jsonb) even when nothing had changed — 2.45M row updates,
~73% of them non-HOT, plus the WAL and dead tuples that go with that.

`upsert_changed` fixes both halves:

* batching: one multi-row ``INSERT ... VALUES (...), (...) ...`` per page
  (default 500 rows) instead of one statement per row. psycopg 3 has no
  ``execute_values``; a multi-row VALUES list is the equivalent.
* change filter: ``ON CONFLICT DO UPDATE ... WHERE (cols) IS DISTINCT FROM
  (excluded cols) OR <touch_col> is older than <touch_interval>``. Unchanged rows
  are skipped entirely (no new tuple version, no WAL); `touch_col` (last_seen)
  is still refreshed at most once per `touch_interval` so it stays meaningful.

`lib/db.upsert` is deliberately left untouched: the settled sweep uses it and
must not change while its catch-up is running.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from psycopg import sql as psql


def _dedupe(rows: Sequence[dict], keys: list[str]) -> list[dict]:
    """Last row wins per conflict key.

    A single INSERT ... ON CONFLICT DO UPDATE cannot affect the same row twice
    ("command cannot affect row a second time"), and pagination can return an
    event twice. executemany tolerated duplicates (last write won); keep that.
    """
    out: dict[tuple, dict] = {}
    for r in rows:
        if r:
            out[tuple(r.get(k) for k in keys)] = r
    return list(out.values())


def build_upsert_sql(table: str, cols: list[str], conflict: list[str], n_rows: int,
                     compare_cols: Iterable[str] | None = None,
                     touch_col: str | None = None, touch_interval: str = "1 day") -> psql.Composed:
    """INSERT ... VALUES (n_rows tuples) ON CONFLICT (...) DO UPDATE SET ... WHERE <changed>
    RETURNING (xmax = 0) — true for inserted rows, false for updated rows.
    Rows skipped by the WHERE are not returned."""
    updates = [c for c in cols if c not in conflict]
    compare = list(compare_cols) if compare_cols is not None else [c for c in updates if c != touch_col]
    missing = [c for c in compare if c not in updates]
    if missing:
        raise ValueError(f"compare columns not written by this upsert: {missing}")
    if not updates:
        raise ValueError("nothing to update")
    t = psql.Identifier(table)
    row = psql.SQL("(") + psql.SQL(", ").join([psql.Placeholder()] * len(cols)) + psql.SQL(")")
    cond = [psql.SQL("({}) is distinct from ({})").format(
        psql.SQL(", ").join(psql.SQL("{}.{}").format(t, psql.Identifier(c)) for c in compare),
        psql.SQL(", ").join(psql.SQL("excluded.{}").format(psql.Identifier(c)) for c in compare),
    )] if compare else []
    if touch_col:
        tc = psql.Identifier(touch_col)
        cond.append(psql.SQL("{t}.{c} is null or {t}.{c} < excluded.{c} - {iv}::interval").format(
            t=t, c=tc, iv=psql.Literal(touch_interval)))
    if not cond:
        raise ValueError("no change condition")
    return psql.SQL(
        "insert into {t} ({cols}) values {rows} on conflict ({conflict}) do update set {sets} "
        "where {cond} returning (xmax = 0)"
    ).format(
        t=t,
        cols=psql.SQL(", ").join(map(psql.Identifier, cols)),
        rows=psql.SQL(", ").join([row] * n_rows),
        conflict=psql.SQL(", ").join(map(psql.Identifier, conflict)),
        sets=psql.SQL(", ").join(psql.SQL("{c} = excluded.{c}").format(c=psql.Identifier(c)) for c in updates),
        cond=psql.SQL(" or ").join(cond),
    )


def upsert_changed(conn, table: str, rows: Sequence[dict], conflict: str,
                   compare_cols: Iterable[str] | None = None, touch_col: str | None = None,
                   touch_interval: str = "1 day", page_size: int = 500) -> dict:
    """Batched change-filtered upsert. Returns counts:
    {"seen": rows offered (after dedupe), "inserted": n, "updated": n, "unchanged": n}.

    `compare_cols` defaults to every written non-key column except `touch_col`,
    so a column added to the row dict is compared automatically.
    """
    keys = [k.strip() for k in conflict.split(",")]
    rows = _dedupe(rows, keys)
    stats = {"seen": len(rows), "inserted": 0, "updated": 0, "unchanged": 0}
    if not rows:
        return stats
    cols = list(rows[0].keys())
    compare = list(compare_cols) if compare_cols is not None else None
    cached: dict[int, psql.Composed] = {}
    with conn.cursor() as cur:
        for i in range(0, len(rows), page_size):
            page = rows[i:i + page_size]
            q = cached.get(len(page))
            if q is None:
                q = cached[len(page)] = build_upsert_sql(table, cols, keys, len(page), compare, touch_col, touch_interval)
            params = [r.get(c) for r in page for c in cols]
            cur.execute(q, params)
            res = cur.fetchall()
            ins = sum(1 for (x,) in res if x)
            stats["inserted"] += ins
            stats["updated"] += len(res) - ins
            stats["unchanged"] += len(page) - len(res)
    return stats


# ---------------------------------------------------------------- market_snapshots (DB sweep #4, 2026-11-20)
SNAPSHOT_COLS = [
    ("ticker", "text"), ("run_ts", "timestamptz"), ("series_ticker", "text"), ("event_ticker", "text"),
    ("status", "text"), ("yes_bid", "numeric"), ("yes_ask", "numeric"), ("no_bid", "numeric"), ("no_ask", "numeric"),
    ("last_price", "numeric"), ("volume", "numeric"), ("volume_24h", "numeric"), ("open_interest", "numeric"),
    ("liquidity", "numeric"), ("price_level_structure", "text"), ("close_time", "timestamptz"),
]
SNAPSHOT_KEY = ["ticker", "run_ts"]


def build_snapshot_sql(skip_unchanged: bool) -> psql.Composed:
    """ONE statement per page: insert ... select from unnest(typed arrays). With skip_unchanged, a market whose
    newest stored snapshot inside the refresh window (%s hours) has identical values is not written again.
    Same-minute re-runs still overwrite (on conflict (ticker, run_ts) do update), as before."""
    cols = [c for c, _ in SNAPSHOT_COLS]
    ident = lambda c: psql.Identifier(c)
    arrays = psql.SQL(", ").join(psql.SQL("%s::" + t + "[]") for _, t in SNAPSHOT_COLS)
    compare = [c for c in cols if c not in SNAPSHOT_KEY]
    where = psql.SQL("")
    if skip_unchanged:
        where = psql.SQL(
            " where not exists (select 1 from (select * from market_snapshots p where p.ticker = v.ticker"
            " and p.run_ts < v.run_ts and p.run_ts > v.run_ts - %s::float8 * interval '1 hour'"
            " order by p.run_ts desc limit 1) last where ({lc}) is not distinct from ({vc}))"
        ).format(lc=psql.SQL(", ").join(psql.SQL("last.{}").format(ident(c)) for c in compare),
                 vc=psql.SQL(", ").join(psql.SQL("v.{}").format(ident(c)) for c in compare))
    return psql.SQL(
        "insert into market_snapshots ({cols}) select {vcols} from unnest({arrays}) as v({cols}){where} "
        "on conflict (ticker, run_ts) do update set {sets}"
    ).format(cols=psql.SQL(", ").join(map(ident, cols)),
             vcols=psql.SQL(", ").join(psql.SQL("v.{}").format(ident(c)) for c in cols),
             arrays=arrays, where=where,
             sets=psql.SQL(", ").join(psql.SQL("{c} = excluded.{c}").format(c=ident(c)) for c in compare))


def _coerce(t: str, v):
    if v is None:
        return None
    if t == "numeric":
        from decimal import Decimal
        return v if isinstance(v, Decimal) else Decimal(str(v))
    return v


def insert_snapshots(conn, rows: Sequence[dict], skip_unchanged_hours: float = 0) -> dict:
    """Write one page of market_snapshots rows as ONE statement (was executemany: one statement per row).
    skip_unchanged_hours > 0: skip markets whose newest snapshot in that many hours is identical, so every
    market still gets a fresh row at least every `skip_unchanged_hours` (keep it < 24 h: v_latest_snapshot
    only looks back 24 h). Returns {"sent": n, "written": m, "skipped": n - m}."""
    rows = _dedupe(rows, SNAPSHOT_KEY)
    if not rows:
        return {"sent": 0, "written": 0, "skipped": 0}
    arrays = [[_coerce(t, r.get(c)) for r in rows] for c, t in SNAPSHOT_COLS]
    skip = skip_unchanged_hours and skip_unchanged_hours > 0
    params = arrays + ([float(skip_unchanged_hours)] if skip else [])
    with conn.cursor() as cur:
        cur.execute(build_snapshot_sql(bool(skip)), params)
        written = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)
    return {"sent": len(rows), "written": written, "skipped": len(rows) - written}
