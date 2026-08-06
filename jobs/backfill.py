"""Job C — backfill. One-time, resumable. (build-spec §6)

Two phases:
  1. ingest 180 days of settled markets into markets_terminal
  2. aggregate their tapes in ascending volume order (long tail first)

Resumable at both phases: phase 1 upserts, phase 2 skips tickers already in
market_taker_stats. Safe to kill and rerun.

Caps, per §6:
  - page cap 50 per market (50k trades); beyond that truncated=true and move on
  - BACKFILL_EXCLUDE_CATEGORIES to shed request budget

One documented deviation from §6: markets with volume below BACKFILL_MIN_VOLUME
(default 1) are skipped. §6 says ascending volume order, and we keep that — but
a zero-volume market has an empty tape, so aggregating it costs a request and
returns nothing. Over 180 days that is tens of thousands of wasted requests for
zero signal. The floor is env-tunable; set it to 0 to follow §6 literally.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from lib import db, mappers, tape  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("backfill")
JOB = "backfill"
BATCH = 200
DAYS = int(os.environ.get("BACKFILL_DAYS", "180"))
MIN_VOLUME = float(os.environ.get("BACKFILL_MIN_VOLUME",
                                 str(tape.MIN_VOLUME)))


def excluded_categories() -> set[str]:
    raw = os.environ.get("BACKFILL_EXCLUDE_CATEGORIES", "")
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def event_series_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("select event_ticker, series_ticker from events where series_ticker is not null")
        return dict(cur.fetchall())


def recurring_series(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            select series_ticker from series
             where coalesce(frequency, '') not in ('one_off', '')
             order by series_ticker
        """)
        return [r[0] for r in cur.fetchall()]


def ingest_archive(conn, client: KalshiClient, since: datetime) -> int:
    """Phase 1 — walk the deep archive per series (D26).

    `/markets` only exposes a rolling recent window (~75 days on the series
    measured, and *nothing at all* for slower series like KXJOBLESS), so
    ingesting a 180-day backfill from it silently under-collects. The archive
    endpoint has no time filter, but returns newest-first — so we stop as soon
    as a page is entirely older than the window.

    Restricted to recurring series: v_screen requires recurrence='recurring',
    so one-off series can never surface and are not worth the walk.
    """
    ev_map = event_series_map(conn)
    tickers = recurring_series(conn)
    log.info("archive walk over %d recurring series", len(tickers))

    total, batch = 0, []
    for i, st in enumerate(tickers, 1):
        kept_any = False
        for page, markets in enumerate(
                _pages(client.iter_historical_markets(st))):
            in_window = 0
            for m in markets:
                if m.get("status") not in mappers.TERMINAL_STATUSES:  # D4
                    continue
                s_ts = mappers.ts(m.get("settlement_ts")) or mappers.ts(m.get("close_time"))
                if s_ts and s_ts < since:
                    continue
                in_window += 1
                batch.append(mappers.terminal_row(m, ev_map.get(m.get("event_ticker")) or st))
            kept_any |= in_window > 0
            if in_window == 0 and page > 0:
                break  # newest-first: an all-stale page means we're past the window
            if len(batch) >= BATCH:
                total += db.upsert(conn, "markets_terminal", batch, conflict="ticker")
                conn.commit()
                batch = []
        if i % 100 == 0:
            log.info("archive: %d/%d series, %d markets, %d requests",
                     i, len(tickers), total, client.request_count)

    if batch:
        total += db.upsert(conn, "markets_terminal", batch, conflict="ticker")
        conn.commit()

    repaired = tape.repair_series_links(conn)
    conn.commit()
    log.info("archive ingest complete: %d markets, %d series links repaired", total, repaired)
    return total


def _pages(items):
    """Regroup a flat item iterator into API-page-sized chunks."""
    buf = []
    for it in items:
        buf.append(it)
        if len(buf) >= 1000:
            yield buf
            buf = []
    if buf:
        yield buf


def ingest(conn, client: KalshiClient, since: datetime) -> int:
    """Phase 1 (recent window only) — page settled markets from /markets.

    Cheaper than the archive walk because it is a single global page-walk, but
    it only reaches back as far as the live endpoint's retention (D26). Use
    ingest_archive() for real depth.
    """
    ev_map = event_series_map(conn)
    min_settled_ts = int(since.timestamp())  # D6
    total, batch = 0, []

    for m in client.iter_markets(status="settled", min_settled_ts=min_settled_ts):
        if m.get("status") not in mappers.TERMINAL_STATUSES:  # D4
            continue
        batch.append(mappers.terminal_row(m, ev_map.get(m.get("event_ticker"))))
        if len(batch) >= BATCH:
            total += db.upsert(conn, "markets_terminal", batch, conflict="ticker")
            conn.commit()
            batch = []
            log.info("ingested %d markets", total)
    if batch:
        total += db.upsert(conn, "markets_terminal", batch, conflict="ticker")
        conn.commit()

    repaired = tape.repair_series_links(conn)
    conn.commit()
    log.info("phase 1 complete: %d terminal markets, %d series links repaired",
             total, repaired)
    return total


def pending_markets(conn, since: datetime, exclude: set[str]) -> list[dict]:
    """Phase 2 worklist: not yet aggregated, ascending volume (§6)."""
    sql = """
        select mt.ticker, mt.result, mt.series_ticker, mt.volume, coalesce(s.category, '')
        from markets_terminal mt
        left join series s on s.series_ticker = mt.series_ticker
        left join market_taker_stats st on st.ticker = mt.ticker
        where st.ticker is null
          and mt.settlement_ts >= %s
          and coalesce(mt.volume, 0) >= %s
        order by mt.volume asc nulls first
    """
    with conn.cursor() as cur:
        cur.execute(sql, (since, MIN_VOLUME))
        rows = cur.fetchall()

    out = []
    for ticker, result, series_ticker, volume, category in rows:
        if category.lower() in exclude:
            continue
        out.append({"ticker": ticker, "result": result, "series_ticker": series_ticker,
                    "volume": volume})
    return out


def aggregate_all(conn, client: KalshiClient, markets: list[dict], page_cap: int) -> tuple[int, int]:
    fee_mults = tape.fee_multipliers(conn)
    done = truncated = 0

    for i, m in enumerate(markets, 1):
        st = tape.process_market(conn, client, m, fee_mults, page_cap)
        done += 1
        truncated += bool(st.truncated)
        if done % 25 == 0:
            conn.commit()
            db.set_job_cursor(conn, JOB, cursor_text=m["ticker"],
                              notes={"aggregated": done, "remaining": len(markets) - i})
            conn.commit()
            log.info("aggregated %d/%d (%d truncated) — %d requests",
                     done, len(markets), truncated, client.request_count)
    conn.commit()
    return done, truncated


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    page_cap = int(os.environ.get("TAPE_PAGE_CAP", tape.DEFAULT_PAGE_CAP))
    since = datetime.now(timezone.utc) - timedelta(days=DAYS)
    exclude = excluded_categories()
    client = KalshiClient()

    log.info("backfill window: %s .. now (%d days), min_volume=%s, exclude=%s",
             since.date(), DAYS, MIN_VOLUME, sorted(exclude) or "none")

    with db.connect() as conn:
        if os.environ.get("BACKFILL_SKIP_INGEST", "").lower() not in ("1", "true", "yes"):
            # Archive-first: /markets alone silently caps at the live retention
            # window and returns nothing for slower series (D26). The recent
            # walk still runs — it is one cheap global page-walk and covers
            # anything listed since the archive was last written.
            if os.environ.get("BACKFILL_SOURCE", "archive").lower() != "recent":
                ingest_archive(conn, client, since)
            ingest(conn, client, since)

        markets = pending_markets(conn, since, exclude)
        log.info("phase 2 worklist: %d markets", len(markets))
        done, truncated = aggregate_all(conn, client, markets, page_cap)
        db.set_job_cursor(conn, JOB, cursor_ts=datetime.now(timezone.utc),
                          notes={"aggregated": done, "truncated": truncated, "complete": True})

    log.info("done: %d aggregated, %d truncated, %d requests", done, truncated, client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
