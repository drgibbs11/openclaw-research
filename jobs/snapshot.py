"""Job A — snapshot. Cron: 0 */6 * * *  (build-spec §6)

Upserts series and events, then appends one market_snapshots row per open
market with run_ts = job start truncated to the minute (G3: re-running within
the same minute overwrites rather than duplicating).

Markets carry no series_ticker (D14), so linkage is resolved market -> event ->
series using the events table.

2026-10-23 perf fix: events are written with lib.bulk.upsert_changed (multi-row
VALUES, 500/page, DO UPDATE only when a column actually changed or last_seen is
>1 day old) and committed per page, so the ~55-minute events phase no longer
holds one transaction open or rewrites ~900k unchanged rows. Market -> series
linkage looks up only the event tickers in each market page (index-only scan on
ev_ticker_series_cov, sql/040) instead of loading the whole events table.
The market_snapshots phase is unchanged and still one transaction.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from lib import bulk, db, mappers  # noqa: E402
from lib.kalshi import KalshiClient, RateLimited  # noqa: E402

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


# Every column event_row writes except the key and last_seen; an update only
# happens when one of these differs (or last_seen is over a day old).
EVENT_COMPARE_COLS = ("series_ticker", "title", "category", "mutually_exclusive",
                      "settlement_sources", "raw")


def write_events(conn, batch: list[dict], totals: dict) -> None:
    st = bulk.upsert_changed(conn, "events", batch, conflict="event_ticker",
                             compare_cols=EVENT_COMPARE_COLS, touch_col="last_seen",
                             touch_interval="1 day", page_size=BATCH)
    conn.commit()  # short transactions: nothing idles in-transaction across API calls
    for k, v in st.items():
        totals[k] += v


def sync_events(conn, client: KalshiClient, now: datetime) -> int:
    totals = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0}
    batch = []
    try:
        for e in client.iter_events():
            if not e.get("event_ticker"):
                continue
            batch.append(mappers.event_row(e, now))
            if len(batch) >= BATCH:
                write_events(conn, batch, totals)
                batch = []
        if batch:
            write_events(conn, batch, totals)
    except Exception as exc:
        # Pages already went through write_events, which commits. main() logs
        # this and still snapshots. The attribute travels with the exception
        # so that warning can include the counts. A slotted exception that
        # refuses the attribute still propagates unchanged.
        try:
            exc.events_committed = dict(totals)
        except Exception:
            pass
        raise
    log.info("events upserted: %d (inserted %d, updated %d, unchanged/skipped %d)",
             totals["seen"], totals["inserted"], totals["updated"], totals["unchanged"])
    return totals["seen"]


def _events_failure_warning(exc: BaseException, cursor: str | None) -> str:
    """One line for the log: what failed, how far events got, and that we continue."""
    committed = getattr(exc, "events_committed", None) or {}
    parts = [f"events sync failed ({type(exc).__name__}: {exc})"]
    if committed:
        parts.append(
            f"after {committed.get('seen', 0)} committed events "
            f"(inserted {committed.get('inserted', 0)}, "
            f"updated {committed.get('updated', 0)}, "
            f"unchanged {committed.get('unchanged', 0)})"
        )
    if cursor:
        parts.append(f"resume cursor {cursor}")
    parts.append("continuing to market snapshots with events already stored")
    return "; ".join(parts)


def lookup_series(conn, event_tickers, cache: dict) -> None:
    """Fill `cache` with event_ticker -> series_ticker (None when unknown) for the
    given tickers not already cached. Replaces the old full-table event_series_map."""
    need = sorted({t for t in event_tickers if t and t not in cache})
    if not need:
        return
    with conn.cursor() as cur:
        cur.execute("select event_ticker, series_ticker from events "
                    "where event_ticker = any(%s) and series_ticker is not null", (need,))
        found = dict(cur.fetchall())
    for t in need:
        cache[t] = found.get(t)


def snapshot_markets(conn, client: KalshiClient, run_ts: datetime) -> int:
    ev_cache: dict[str, str | None] = {}
    total, batch, unmapped = 0, [], 0

    def flush(markets: list[dict]) -> int:
        nonlocal unmapped
        lookup_series(conn, (m.get("event_ticker") for m in markets), ev_cache)
        rows = []
        for m in markets:
            st = ev_cache.get(m.get("event_ticker"))
            if st is None:
                unmapped += 1
            rows.append(mappers.snapshot_row(m, run_ts, st))
        return db.upsert(conn, "market_snapshots", rows, conflict="ticker, run_ts")

    # D8: mve_filter=exclude — combo markets are not screenable series.
    for m in client.iter_markets(status="open"):
        if not m.get("ticker"):
            continue
        batch.append(m)
        if len(batch) >= BATCH:
            total += flush(batch)
            batch = []
    if batch:
        total += flush(batch)

    log.info("snapshots written: %d (unmapped series: %d)", total, unmapped)
    return total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    client = KalshiClient()

    with db.connect() as conn:
        sync_series(conn, client, run_ts)
        conn.commit()
        # Events are reference data, committed per page. A Kalshi 429 (or any
        # other events failure) must not skip market_snapshots — that phase is
        # the time-critical write, and it can use whatever events are already
        # stored. RateLimited is the observed failure; Exception covers a
        # failed events sync generally. Snapshot errors still propagate.
        events_error: BaseException | None = None
        events_cursor: str | None = None
        try:
            sync_events(conn, client, run_ts)  # commits per page
        except RateLimited as exc:
            events_error = exc
        except Exception as exc:
            events_error = exc
        if events_error is not None:
            events_cursor = getattr(client, "page_cursor", None)
            log.warning("%s", _events_failure_warning(events_error, events_cursor))
            conn.rollback()
        n = snapshot_markets(conn, client, run_ts)
        notes: dict = {"snapshots": n}
        # The next run still walks /events from the first page. Upserts are
        # idempotent, and skipping ahead would miss changes on earlier pages.
        # cursor_text records where this run stopped so that point isn't only
        # in a log line.
        cursor_text = None
        if events_error is not None:
            notes["events_sync"] = type(events_error).__name__
            notes["events_error"] = str(events_error)
            committed = getattr(events_error, "events_committed", None)
            if committed:
                notes["events_committed"] = committed
            if events_cursor:
                notes["events_cursor"] = events_cursor
                cursor_text = events_cursor
        db.set_job_cursor(
            conn, "snapshot", cursor_ts=run_ts, cursor_text=cursor_text, notes=notes,
        )

    log.info("done in %d requests", client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
