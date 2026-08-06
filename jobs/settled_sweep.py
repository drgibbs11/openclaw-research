"""Job B — settled sweep. Cron: 30 6 * * *  (build-spec §6)

Pages newly-settled markets since the last high-water mark, upserts them into
markets_terminal, then aggregates each tape into market_taker_stats.

D7 removed this job's main cost risk: `min_settled_ts` is a server-side filter
and markets carry `settlement_ts`, so the high-water mark is a query parameter
rather than a client-side scan of every settled market ever listed.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from lib import db, mappers, tape  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("settled_sweep")
JOB = "settled_sweep"
BATCH = 200
LOOKBACK_DEFAULT_DAYS = 3  # first run with no cursor
OVERLAP = timedelta(hours=2)  # re-scan a little; upserts make it free (G3)


def event_series_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("select event_ticker, series_ticker from events where series_ticker is not null")
        return dict(cur.fetchall())


def sweep(conn, client: KalshiClient, since: datetime, page_cap: int) -> tuple[int, int]:
    ev_map = event_series_map(conn)
    fee_mults = tape.fee_multipliers(conn)
    done = tape.existing_stats(conn)

    min_settled_ts = int(since.timestamp())  # D6: unix seconds
    markets, batch, ingested, skipped_novol = [], [], 0, 0

    for m in client.iter_markets(status="settled", min_settled_ts=min_settled_ts):
        if m.get("status") not in mappers.TERMINAL_STATUSES:  # D4
            continue
        st = ev_map.get(m.get("event_ticker"))
        batch.append(mappers.terminal_row(m, st))
        ingested += 1

        # Ingest every terminal market, but only spend a tape request where
        # there is a tape to read. ~76% of settled markets have zero volume.
        if float(m.get("volume_fp") or 0) >= tape.MIN_VOLUME:
            markets.append({"ticker": m["ticker"], "result": m.get("result"),
                            "series_ticker": st,
                            "event_ticker": m.get("event_ticker")})
        else:
            skipped_novol += 1

        if len(batch) >= BATCH:
            db.upsert(conn, "markets_terminal", batch, conflict="ticker")
            conn.commit()
            batch = []
    if batch:
        db.upsert(conn, "markets_terminal", batch, conflict="ticker")
        conn.commit()

    # Any market whose event arrived after it did would otherwise keep a NULL
    # series_ticker and never reach the screen. Resolve the events we actually
    # need (only for markets we'll aggregate), then relink.
    unlinked = {m["event_ticker"] for m in markets
                if m["event_ticker"] and not m["series_ticker"]}
    tape.resolve_missing_events(conn, client, unlinked)
    conn.commit()
    repaired = tape.repair_series_links(conn)
    conn.commit()

    # Reflect the repair back onto the in-memory worklist so this run's tape
    # aggregation attributes fees to the right series.
    if repaired:
        ev_map = event_series_map(conn)
        for m in markets:
            if not m["series_ticker"]:
                m["series_ticker"] = ev_map.get(m["event_ticker"])

    log.info("terminal ingested: %d (%d zero-volume, tape queue %d, %d series links repaired)",
             ingested, skipped_novol, len(markets), repaired)

    aggregated = 0
    for m in markets:
        if m["ticker"] in done:
            continue
        st = tape.process_market(conn, client, m, fee_mults, page_cap)
        aggregated += 1
        if aggregated % 25 == 0:
            conn.commit()
            log.info("aggregated %d/%d (last: %s, %d trades)",
                     aggregated, len(markets), m["ticker"], st.trades)
    conn.commit()
    return ingested, aggregated


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    page_cap = int(os.environ.get("TAPE_PAGE_CAP", tape.DEFAULT_PAGE_CAP))
    client = KalshiClient()
    started = datetime.now(timezone.utc)

    with db.connect() as conn:
        cursor_ts, _ = db.get_job_cursor(conn, JOB)
        since = (cursor_ts - OVERLAP) if cursor_ts else started - timedelta(days=LOOKBACK_DEFAULT_DAYS)
        log.info("sweeping settlements since %s", since.isoformat())

        n_markets, n_agg = sweep(conn, client, since, page_cap)
        db.set_job_cursor(conn, JOB, cursor_ts=started,
                          notes={"markets": n_markets, "aggregated": n_agg})

    log.info("done: %d markets, %d aggregated, %d requests",
             n_markets, n_agg, client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
