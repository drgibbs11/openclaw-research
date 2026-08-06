"""Job A — snapshot. Cron: 0 */6 * * *  (build-spec §6)

Upserts series and events, then appends one market_snapshots row per open
market with run_ts = job start truncated to the minute (G3: re-running within
the same minute overwrites rather than duplicating).

Markets carry no series_ticker (D14), so linkage is resolved market -> event ->
series using the events table.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from lib import db, mappers  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("snapshot")
BATCH = 500


def sync_series(conn, client: KalshiClient, now: datetime) -> int:
    """D5: one unpaginated call returns every series (~12.5k)."""
    series = client.list_series()
    rows = [mappers.series_row(s, now) for s in series if s.get("ticker")]
    for i in range(0, len(rows), BATCH):
        db.upsert(conn, "series", rows[i:i + BATCH], conflict="series_ticker")
    log.info("series upserted: %d", len(rows))
    return len(rows)


def sync_events(conn, client: KalshiClient, now: datetime) -> int:
    total, batch = 0, []
    for e in client.iter_events():
        if not e.get("event_ticker"):
            continue
        batch.append(mappers.event_row(e, now))
        if len(batch) >= BATCH:
            total += db.upsert(conn, "events", batch, conflict="event_ticker")
            batch = []
    if batch:
        total += db.upsert(conn, "events", batch, conflict="event_ticker")
    log.info("events upserted: %d", total)
    return total


def event_series_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("select event_ticker, series_ticker from events where series_ticker is not null")
        return dict(cur.fetchall())


def snapshot_markets(conn, client: KalshiClient, run_ts: datetime) -> int:
    ev_map = event_series_map(conn)
    total, batch, unmapped = 0, [], 0

    # D8: mve_filter=exclude — combo markets are not screenable series.
    for m in client.iter_markets(status="open"):
        if not m.get("ticker"):
            continue
        st = ev_map.get(m.get("event_ticker"))
        if st is None:
            unmapped += 1
        batch.append(mappers.snapshot_row(m, run_ts, st))
        if len(batch) >= BATCH:
            total += db.upsert(conn, "market_snapshots", batch, conflict="ticker, run_ts")
            batch = []
    if batch:
        total += db.upsert(conn, "market_snapshots", batch, conflict="ticker, run_ts")

    log.info("snapshots written: %d (unmapped series: %d)", total, unmapped)
    return total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    client = KalshiClient()

    with db.connect() as conn:
        sync_series(conn, client, run_ts)
        sync_events(conn, client, run_ts)
        n = snapshot_markets(conn, client, run_ts)
        db.set_job_cursor(conn, "snapshot", cursor_ts=run_ts, notes={"snapshots": n})

    log.info("done in %d requests", client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
