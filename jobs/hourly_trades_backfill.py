"""Job A — per-trade tape backfill for the hourly temperature series.

One-time and resumable. Walks every settled market in markets_terminal whose
series has persist_trades and whose result is yes/no, reads its public tape
once, and writes both the raw fills (market_trades) and the aggregate
(market_taker_stats) from that single read.

Resumability is by market, ordered by close_time, with the last completed
close_time held in job_state. That is cheaper than tracking a ticker set and
survives the run being killed mid-ladder: the overlap re-reads at most one
hour of markets, and every write is an upsert keyed on the trade id, so a
re-read costs requests and changes nothing.

Cap: MAX_TRADES (default 2,000,000). Expected ~560k from
sum(market_taker_stats.trades) over the same tickers.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from lib import db, tape  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("hourly_trades_backfill")
JOB = "hourly_trades_backfill"
MAX_TRADES = int(os.environ.get("HOURLY_MAX_TRADES", "2000000"))
COMMIT_EVERY = 50
OVERLAP = timedelta(hours=1)


def worklist(conn, since: datetime | None, limit: int | None):
    """Settled hourly markets with a tape worth reading, oldest first.

    volume > 0 is the filter that keeps this affordable: a market that never
    traded has an empty tape, and reading it costs a request to learn nothing.
    """
    sql = """
        select mt.ticker, mt.series_ticker, mt.result, mt.close_time
          from markets_terminal mt
          join series s on s.series_ticker = mt.series_ticker
         where s.persist_trades
           and mt.result in ('yes','no')
           and coalesce(mt.volume, 0) > 0
    """
    params: list = []
    if since is not None:
        sql += " and mt.close_time >= %s"
        params.append(since)
    sql += " order by mt.close_time, mt.ticker"
    if limit:
        sql += " limit %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def trades_stored(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from market_trades")
        return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after this many markets (smoke runs)")
    ap.add_argument("--restart", action="store_true",
                    help="ignore the stored cursor and start from the beginning")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    page_cap = int(os.environ.get("TAPE_PAGE_CAP", tape.DEFAULT_PAGE_CAP))
    client = KalshiClient()

    with db.connect() as conn:
        persist = tape.persist_series(conn)
        if not persist:
            log.error("no series has persist_trades — run sql/040_hourly.sql")
            return 1
        fee_mults = tape.fee_multipliers(conn)

        cursor_ts, _ = db.get_job_cursor(conn, JOB)
        since = None if args.restart else (cursor_ts - OVERLAP if cursor_ts else None)
        markets = worklist(conn, since, args.limit)
        log.info("persist series: %s", ", ".join(sorted(persist)))
        log.info("%d market(s) to read%s", len(markets),
                 f" since {since.isoformat()}" if since else "")

        stored = trades_stored(conn)
        done = 0
        last_close = None

        for ticker, series, result, close_time in markets:
            if stored >= MAX_TRADES:
                log.warning("cap reached: %d trades stored, stopping", stored)
                break
            try:
                st = tape.process_market(
                    conn, client,
                    {"ticker": ticker, "series_ticker": series, "result": result},
                    fee_mults, page_cap, persist=persist)
            except Exception as exc:
                # One unreadable market must not end a multi-hour run. The
                # cursor is not advanced past it, so the next run retries it.
                log.error("%s: %s", ticker, exc)
                continue

            done += 1
            stored += st.trades
            last_close = close_time

            if done % COMMIT_EVERY == 0:
                conn.commit()
                db.set_job_cursor(conn, JOB, cursor_ts=last_close,
                                  notes={"markets": done, "trades_stored": stored})
                conn.commit()
                log.info("%d/%d markets, ~%d trades stored (last %s)",
                         done, len(markets), stored, ticker)

        conn.commit()
        if last_close is not None:
            db.set_job_cursor(conn, JOB, cursor_ts=last_close,
                              notes={"markets": done, "trades_stored": stored})
        final = trades_stored(conn)

    log.info("done: %d markets, %d trades in market_trades, %d requests",
             done, final, client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
