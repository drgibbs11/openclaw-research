"""Retention — deletes aged market_snapshots rows. Cron: 0 5 * * *

Replaces `psql -f sql/020_prune.sql`. That file uses psql meta-commands
(\\if, \\set) which only the psql *client* understands, and the Nixpacks Python
image Railway builds does not ship psql — so the SQL version cannot run as a
Railway cron step at all. This does the same work over psycopg.

market_snapshots is the only append-only table that grows without bound:
~78k open markets x 4 runs/day x ~296 bytes = ~92 MB/day. v_series_activity
reads only the last 24 hours; everything older is history. 30 days caps the
table at roughly 2.6 GB.

Two things this does that a bare DELETE does not:

  - Deletes in batches. A single DELETE covering a month of a multi-GB table
    holds one long transaction and can trip a statement timeout, leaving the
    whole thing rolled back after doing all the work. Batches commit as they
    go, so a timeout or a kill costs one batch, and the next run resumes.
  - VACUUMs afterwards, in autocommit. A DELETE only marks tuples dead; without
    a vacuum the heap keeps its size and the footprint only ever grows.

  PRUNE_DAYS       retention window, default 30
  PRUNE_BATCH      rows per transaction, default 200000
  PRUNE_SKIP_VACUUM  set to skip the vacuum (see the pooler note in main)
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, ".")

from lib import db  # noqa: E402

log = logging.getLogger("prune")

DAYS = int(os.environ.get("PRUNE_DAYS", "30"))
BATCH = int(os.environ.get("PRUNE_BATCH", "200000"))


def table_size(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("select pg_size_pretty(pg_total_relation_size('market_snapshots'))")
        return cur.fetchone()[0]


def prune(conn, days: int, batch: int) -> int:
    """Delete in bounded batches, committing each. Returns rows removed."""
    total = 0
    while True:
        with conn.cursor() as cur:
            # ctid-keyed subselect so each statement touches a bounded number of
            # rows and can use ms_run_ts rather than scanning the heap.
            cur.execute(
                """
                delete from market_snapshots
                 where ctid in (
                   select ctid from market_snapshots
                    where run_ts < now() - make_interval(days => %s)
                    limit %s
                 )
                """,
                (days, batch),
            )
            n = cur.rowcount
        conn.commit()
        total += n
        if n:
            log.info("deleted %d (running total %d)", n, total)
        if n < batch:
            return total


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    with db.connect() as conn:
        before = table_size(conn)
        log.info("market_snapshots %s, pruning older than %d days", before, DAYS)
        removed = prune(conn, DAYS, BATCH)
        after_delete = table_size(conn)

    log.info("removed %d rows (%s -> %s before vacuum)", removed, before, after_delete)

    if os.environ.get("PRUNE_SKIP_VACUUM", "").lower() in ("1", "true", "yes"):
        log.info("skipping vacuum (PRUNE_SKIP_VACUUM)")
        return 0
    if not removed:
        log.info("nothing deleted, skipping vacuum")
        return 0

    # VACUUM cannot run inside a transaction block, hence autocommit. Note this
    # is also the one statement here that behaves badly through Supabase's
    # transaction pooler (port 6543) — if DATABASE_URL points at the pooler and
    # this errors, either point the prune service at the direct connection
    # (5432) or set PRUNE_SKIP_VACUUM and let autovacuum handle it.
    try:
        with db.connect(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("vacuum (analyze) market_snapshots")
            log.info("vacuumed, now %s", table_size(conn))
    except Exception as exc:  # noqa: BLE001 — the delete already committed
        log.warning("vacuum failed (%s); rows are deleted, autovacuum will "
                    "reclaim the space", exc)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
