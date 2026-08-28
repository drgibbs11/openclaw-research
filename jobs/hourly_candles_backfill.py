"""Job B — 1-minute candle backfill for the hourly temperature series.

Scope, per spec: the window is [open_time, close_time] and the market's whole
life is 60 minutes, so there is nothing to prune by time. ALL strikes are
loaded, including the ones that settled worthless — filtering by settled value
would be lookahead and would delete exactly the rows a fair backtest needs.

Rows are change-only: a minute is written if it traded or the touch moved.
The API is already sparse (a 60-minute market returned 26 candles) but sparse
is not change-only — it still returns quiet minutes with unchanged quotes.

Endpoint selection is by the live/historical cutoff, fetched at startup. A
market that settled before it is served ONLY by /historical/..., which returns
the same data under different field names (D38). lib.hourly.candle_row accepts
either; getting this wrong writes a full set of NULL-priced rows rather than
failing, which is why the loader logs the split.

Ordering: run this AFTER the trades backfill and after reviewing
v_hourly_bleed_by_minute. If the bleed map is flat inside the last 30 minutes
there is nothing for candles to refine and this job should not be run at all.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from lib import db  # noqa: E402
from lib.hourly import candle_row, is_change_row  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("hourly_candles_backfill")
JOB = "hourly_candles_backfill"
MAX_ROWS = int(os.environ.get("HOURLY_MAX_CANDLE_ROWS", "1000000"))
COMMIT_EVERY = 100
OVERLAP = timedelta(hours=1)

# MIA settles against an unnamed "Synoptic Data" source our feed reproduces 30%
# of the time, against 97-99% elsewhere. It is excluded from everything
# downstream, so paying for its candles buys nothing.
DEFAULT_SERIES = ("KXTEMPNYCH", "KXTEMPLAXH", "KXTEMPDCH", "KXTEMPCHIH", "KXTEMPAUSH")


def worklist(conn, series: tuple[str, ...], since: datetime | None, limit: int | None):
    sql = """
        select mt.ticker, mt.series_ticker, mt.open_time, mt.close_time
          from markets_terminal mt
         where mt.series_ticker = any(%s)
           and mt.result in ('yes','no')
           and mt.open_time is not null and mt.close_time is not null
    """
    params: list = [list(series)]
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


def load_market(conn, client: KalshiClient, ticker: str, series: str,
                open_time: datetime, close_time: datetime,
                historical: bool) -> int:
    cs = client.candlesticks(ticker, series,
                             int(open_time.timestamp()),
                             int(close_time.timestamp()),
                             period_interval=1, historical=historical)
    rows, prev = [], None
    for c in cs:
        row = candle_row(c, ticker)
        if row is None:
            continue
        if is_change_row(row, prev):
            rows.append(row)
        prev = row
    if rows:
        db.upsert(conn, "market_candles_1m", rows,
                  conflict="market_ticker, bucket_time")
    return len(rows)


def candles_stored(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from market_candles_1m")
        return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=list(DEFAULT_SERIES))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--restart", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = KalshiClient()

    cutoff_raw = client.historical_cutoff().get("market_settled_ts")
    cutoff = datetime.fromisoformat(str(cutoff_raw).replace("Z", "+00:00"))
    log.info("historical cutoff: %s (markets settling before this use /historical)",
             cutoff.isoformat())

    with db.connect() as conn:
        cursor_ts, _ = db.get_job_cursor(conn, JOB)
        since = None if args.restart else (cursor_ts - OVERLAP if cursor_ts else None)
        markets = worklist(conn, tuple(args.series), since, args.limit)
        log.info("%d market(s) over %s", len(markets), ", ".join(args.series))

        stored = candles_stored(conn)
        done = n_hist = n_live = 0
        last_close = None

        for ticker, series, open_time, close_time in markets:
            if stored >= MAX_ROWS:
                log.warning("cap reached: %d candle rows, stopping", stored)
                break
            historical = close_time < cutoff
            try:
                n = load_market(conn, client, ticker, series,
                                open_time, close_time, historical)
            except Exception as exc:
                log.error("%s: %s", ticker, exc)
                continue

            stored += n
            done += 1
            last_close = close_time
            n_hist += 1 if historical else 0
            n_live += 0 if historical else 1

            if done % COMMIT_EVERY == 0:
                conn.commit()
                db.set_job_cursor(conn, JOB, cursor_ts=last_close,
                                  notes={"markets": done, "rows": stored})
                conn.commit()
                log.info("%d/%d markets, %d rows (last %s)",
                         done, len(markets), stored, ticker)

        conn.commit()
        if last_close is not None:
            db.set_job_cursor(conn, JOB, cursor_ts=last_close,
                              notes={"markets": done, "rows": stored})
        final = candles_stored(conn)

    log.info("done: %d markets (%d historical, %d live), %d rows, %d requests",
             done, n_hist, n_live, final, client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
