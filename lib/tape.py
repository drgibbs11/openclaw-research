"""Shared tape-aggregation step used by Job B (sweep) and Job C (backfill).

Reads a settled market's public tape, folds it into one market_taker_stats row
via lib.bleed, and writes it. Idempotent (G3).
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from . import db
from .bleed import aggregate
from .kalshi import KalshiClient

log = logging.getLogger("tape")

DEFAULT_PAGE_CAP = 50  # §6 Job C: 50 pages x 1000 = 50k trades

# A market with no volume has an empty tape, so aggregating it costs a request
# and returns nothing. Measured on a 2-day settled window: 106,052 of 140,338
# markets (76%) had zero volume. Applied by Jobs B and C alike.
MIN_VOLUME = float(os.environ.get("TAPE_MIN_VOLUME", "1"))


def repair_series_links(conn) -> int:
    """Fill in markets_terminal.series_ticker from the events table.

    Markets carry no series_ticker (D14); it is resolved market -> event ->
    series. If a market's event was not yet in `events` when the market was
    ingested, the row lands with a NULL series_ticker — and since v_taker_bleed
    groups by series_ticker, that market is invisible to the screen forever.
    Re-running this after an events sync repairs those rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update markets_terminal mt
               set series_ticker = e.series_ticker
              from events e
             where e.event_ticker = mt.event_ticker
               and mt.series_ticker is null
               and e.series_ticker is not null
            """
        )
        return cur.rowcount


def resolve_missing_events(conn, client: KalshiClient, event_tickers,
                           cap: int = 2000) -> int:
    """Fetch events we don't have, so their markets can be linked to a series.

    Job A syncs events wholesale, but a market can settle before the next sync,
    or its event can roll off. Rather than depend on a full events sync having
    run first, resolve the specific events we need.

    This is authoritative, not inferred. `event_ticker.split('-')[0]` looks like
    the series ticker and matches ~97% of the time, but breaks on legacy
    non-KX-prefixed tickers (JOBLESS -> KXJOBLESS, AITURING -> KXAITURING).
    A 3% silent misattribution rate is not acceptable for the linkage the whole
    screen groups by, so we ask the API.
    """
    wanted = {t for t in event_tickers if t}
    if not wanted:
        return 0
    with conn.cursor() as cur:
        cur.execute("select event_ticker from events where event_ticker = any(%s)",
                    (list(wanted),))
        known = {r[0] for r in cur.fetchall()}

    missing = sorted(wanted - known)
    if len(missing) > cap:
        log.warning("%d events unresolved, fetching %d (cap) — rerun to finish",
                    len(missing), cap)
        missing = missing[:cap]

    from datetime import datetime, timezone

    from .mappers import event_row

    now = datetime.now(timezone.utc)
    rows = []
    for et in missing:
        try:
            body = client.get(f"/events/{et}")
            ev = body.get("event") or body
            if ev.get("event_ticker"):
                rows.append(event_row(ev, now))
        except Exception as exc:
            log.warning("could not fetch event %s: %s", et, exc)

    if rows:
        db.upsert(conn, "events", rows, conflict="event_ticker")
    log.info("resolved %d/%d missing events", len(rows), len(missing))
    return len(rows)


def fee_multipliers(conn) -> dict[str, Decimal | None]:
    """series_ticker -> fee_multiplier (D10). Multiplier 0 => series is free."""
    with conn.cursor() as cur:
        cur.execute("select series_ticker, fee_multiplier from series")
        return {t: m for t, m in cur.fetchall()}


def existing_stats(conn) -> set[str]:
    """Tickers already aggregated — Jobs B and C are resumable (§6)."""
    with conn.cursor() as cur:
        cur.execute("select ticker from market_taker_stats")
        return {r[0] for r in cur.fetchall()}


def compute_stats(client: KalshiClient, ticker: str, result: str | None,
                  fee_multiplier: Decimal | None, page_cap: int = DEFAULT_PAGE_CAP,
                  historical: bool = False):
    """Page the tape and fold it. Sets truncated=True at the page cap."""
    trades = client.iter_trades(ticker, historical=historical, max_pages=page_cap)
    st = aggregate(trades, result, fee_multiplier)
    st.truncated = st.pages_read >= page_cap
    return st


def store_stats(conn, ticker: str, st) -> None:
    row = {"ticker": ticker, **st.as_row()}
    db.upsert(conn, "market_taker_stats", [row], conflict="ticker")


def persist_series(conn) -> set[str]:
    """Series whose per-trade tape is kept, not just aggregated."""
    with conn.cursor() as cur:
        cur.execute("select series_ticker from series where persist_trades")
        return {r[0] for r in cur.fetchall()}


def store_trades(conn, ticker: str, series_ticker: str | None,
                 trades: list[dict]) -> int:
    """Write raw fills to market_trades. Idempotent on the trade id (G3).

    A re-run writes nothing new, which is what makes the backfill resumable at
    market granularity without tracking which markets it already did.
    """
    from .hourly import trade_row

    rows = [r for r in (trade_row(t, series_ticker) for t in trades) if r]
    if not rows:
        return 0
    return db.upsert(conn, "market_trades", rows,
                     conflict="kalshi_trade_id", update_cols=[])


def collect_trades(client: KalshiClient, ticker: str,
                   page_cap: int = DEFAULT_PAGE_CAP,
                   historical: bool = False) -> tuple[list[dict], int]:
    """The tape as a list, plus the highest page index reached.

    The page index comes back because the caller has to know whether it hit
    the cap: a truncated tape that reports truncated=False would look like a
    complete one, and CP4 compares this tape against an aggregate that knows
    it was truncated.
    """
    trades, pages = [], 0
    for t, page in client.iter_trades(ticker, historical=historical,
                                      max_pages=page_cap):
        trades.append(t)
        pages = max(pages, page + 1)
    return trades, pages


def process_market(conn, client: KalshiClient, market: dict, fee_mult_by_series: dict,
                   page_cap: int = DEFAULT_PAGE_CAP,
                   persist: set[str] | None = None) -> object:
    """Aggregate one terminal market's tape and persist it.

    D13: markets whose live tape comes back empty are retried against
    /historical/trades — the cutover age between the two endpoints is
    undocumented (U1), so we discover it empirically rather than assume it.

    When the market's series is in `persist`, the fills are also written to
    market_trades. The tape is read ONCE and used for both: aggregating from
    one list and persisting from a second read would double the request cost
    and could disagree if a fill landed between the two.
    """
    ticker = market["ticker"]
    result = market.get("result")
    series = market.get("series_ticker")
    mult = fee_mult_by_series.get(series)
    keep = bool(persist) and series in persist

    if not keep:
        st = compute_stats(client, ticker, result, mult, page_cap)
        if st.trades == 0:
            st = compute_stats(client, ticker, result, mult, page_cap, historical=True)
        store_stats(conn, ticker, st)
        return st

    trades, pages = collect_trades(client, ticker, page_cap)
    if not trades:
        trades, pages = collect_trades(client, ticker, page_cap, historical=True)

    # Fold the same list the rows come from, so market_taker_stats and
    # market_trades can never describe different tapes (CP4). Page indices are
    # replayed so pages_read/truncated stay honest.
    st = aggregate(((t, max(pages - 1, 0)) for t in trades), result, mult)
    st.pages_read = pages
    st.truncated = pages >= page_cap
    store_trades(conn, ticker, series, trades)
    store_stats(conn, ticker, st)
    return st
