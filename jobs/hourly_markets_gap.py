"""Fill gaps in markets_terminal for the hourly temperature series.

Why this job exists, when Job B already sweeps settlements:

markets_terminal has a seven-week hole in KXTEMPNYCH — nothing settles between
2026-06-06 02:00Z and 2026-07-25 12:00Z. Job B is high-water-mark driven, so a
window it was not running for is never revisited; it only ever moves forward.

The hole is not cosmetic. It swallows all three CP2 reference events
(26JUL0318, 26JUL1403, 26JUL1422), it is 7 of the 24 weeks of NYC history that
CP3's basis check and the phase-1 nowcast backtest are supposed to run over,
and it is inside the "FULL history of NYC" the candle loader is scoped to.

The markets are still retrievable: they settled AFTER the historical cutoff
(2026-06-29), so they live on the live /markets endpoint, which accepts
min_close_ts/max_close_ts and reaches back at least to early July. Verified:
600 KXTEMPNYCH markets came back for 2026-07-03 alone.

Idempotent — it upserts, so re-running over an already-complete window writes
the same rows. Ingests markets only; the tape backfill is a separate job.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from lib import db, mappers  # noqa: E402
from lib.hourly import HOURLY_SERIES  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("hourly_markets_gap")
JOB = "hourly_markets_gap"
BATCH = 500
WINDOW = timedelta(days=1)   # one request window per calendar day


def find_gaps(conn, series: str, min_gap_hours: int = 6) -> list[tuple[datetime, datetime]]:
    """Consecutive close_times more than min_gap_hours apart.

    An hourly series should have a market closing every hour, so any jump
    beyond a few hours is a hole rather than a quiet night.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select prev_close, close_time from (
              select close_time,
                     lag(close_time) over (order by close_time) as prev_close
                from (select distinct close_time
                        from markets_terminal
                       where series_ticker = %s and close_time is not null) d
            ) g
            where prev_close is not null
              and close_time - prev_close > make_interval(hours => %s)
            order by prev_close
            """,
            (series, min_gap_hours),
        )
        return [(a, b) for a, b in cur.fetchall()]


def ingest_window(conn, client: KalshiClient, series: str,
                  lo: datetime, hi: datetime) -> int:
    """Ingest every terminal market for one series closing in [lo, hi)."""
    batch, n = [], 0
    for m in client.iter_markets(status="settled",
                                 series_ticker=series,
                                 min_close_ts=int(lo.timestamp()),
                                 max_close_ts=int(hi.timestamp())):
        if m.get("status") not in mappers.TERMINAL_STATUSES:
            continue
        # No TERMINAL_MIN_VOLUME filter here, unlike Job B. A zero-volume rung
        # still settles, and v_hourly_settle needs every rung of a ladder to
        # decide the settled value and the censored flag — dropping the quiet
        # ones would move max(yes strike) and silently corrupt the temperature.
        batch.append(mappers.terminal_row(m, series))
        n += 1
        if len(batch) >= BATCH:
            db.upsert(conn, "markets_terminal", batch, conflict="ticker")
            conn.commit()
            batch = []
    if batch:
        db.upsert(conn, "markets_terminal", batch, conflict="ticker")
        conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default=None,
                    help="one series; default = every hourly series with a gap")
    ap.add_argument("--min-gap-hours", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = KalshiClient()
    started = datetime.now(timezone.utc)
    series_list = [args.series] if args.series else list(HOURLY_SERIES)

    total = 0
    with db.connect() as conn:
        for series in series_list:
            gaps = find_gaps(conn, series, args.min_gap_hours)
            if not gaps:
                log.info("%s: no gaps", series)
                continue
            for lo, hi in gaps:
                span = hi - lo
                log.info("%s: gap %s -> %s (%.1f h)", series,
                         lo.isoformat(), hi.isoformat(),
                         span.total_seconds() / 3600.0)
                if args.dry_run:
                    continue
                # Walk the hole a day at a time. One request window for seven
                # weeks would page for a long time behind a single cursor and
                # lose everything already fetched if it died mid-run.
                cur = lo
                while cur < hi:
                    nxt = min(cur + WINDOW, hi)
                    n = ingest_window(conn, client, series, cur, nxt)
                    total += n
                    if n:
                        log.info("  %s: %d markets", cur.date().isoformat(), n)
                    cur = nxt

        if not args.dry_run:
            db.set_job_cursor(conn, JOB, cursor_ts=started,
                              notes={"markets": total,
                                     "series": series_list})

    log.info("done: %d markets ingested, %d requests", total, client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
