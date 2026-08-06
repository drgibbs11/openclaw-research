"""API object -> DB row mapping.

Every parsed column is a convenience; `raw` is truth (G4). Field names follow
the live API, not build-spec §4 — see DISCREPANCIES.md D1/D2/D5/D14.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import jsonb
from .kalshi import dec

# D4: the filter vocabulary and the payload vocabulary differ. These are the
# payload values that mean "this market is done and settled".
TERMINAL_STATUSES = {"settled", "finalized"}

# D9: recurrence is structural, not inferred. `custom` is the only genuinely
# ambiguous bucket and is the only one the classifier is asked to judge.
RECURRING_FREQUENCIES = {"fifteen_min", "hourly", "daily", "weekly", "monthly", "annual"}


def ts(value) -> datetime | None:
    """RFC3339 -> aware UTC datetime (G5). Tolerates 'Z' and sub-second parts."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def recurrence_from_frequency(frequency: str | None) -> str:
    f = (frequency or "").lower()
    if f == "one_off":
        return "one_off"
    if f in RECURRING_FREQUENCIES:
        return "recurring"
    return "unknown"  # 'custom' and anything unrecognised


def series_row(s: dict, now: datetime) -> dict:
    return {
        "series_ticker": s.get("ticker"),  # D5: source field is `ticker`
        "title": s.get("title"),
        "category": s.get("category"),
        "frequency": s.get("frequency"),
        "tags": jsonb(s.get("tags")),
        "settlement_sources": jsonb(s.get("settlement_sources")),
        "contract_url": s.get("contract_url"),
        "contract_terms_url": s.get("contract_terms_url"),
        "fee_type": s.get("fee_type"),
        "fee_multiplier": s.get("fee_multiplier"),
        "last_seen": now,
        "raw": jsonb(s),
    }


def event_row(e: dict, now: datetime) -> dict:
    return {
        "event_ticker": e.get("event_ticker"),
        "series_ticker": e.get("series_ticker"),  # D14
        "title": e.get("title"),
        "category": e.get("category"),
        "mutually_exclusive": e.get("mutually_exclusive"),
        "settlement_sources": jsonb(e.get("settlement_sources")),
        "last_seen": now,
        "raw": jsonb(e),
    }


def snapshot_row(m: dict, run_ts: datetime, series_ticker: str | None) -> dict:
    """D1/D2: prices are decimal-string dollars, counts are fractional."""
    return {
        "ticker": m.get("ticker"),
        "run_ts": run_ts,
        "series_ticker": series_ticker,
        "event_ticker": m.get("event_ticker"),
        "status": m.get("status"),
        "yes_bid": dec(m.get("yes_bid_dollars")),
        "yes_ask": dec(m.get("yes_ask_dollars")),
        "no_bid": dec(m.get("no_bid_dollars")),
        "no_ask": dec(m.get("no_ask_dollars")),
        "last_price": dec(m.get("last_price_dollars")),
        "volume": dec(m.get("volume_fp")),
        "volume_24h": dec(m.get("volume_24h_fp")),
        "open_interest": dec(m.get("open_interest_fp")),
        "liquidity": dec(m.get("liquidity_dollars")),
        "price_level_structure": m.get("price_level_structure"),  # D3
        "close_time": ts(m.get("close_time")),
    }


def terminal_row(m: dict, series_ticker: str | None) -> dict:
    return {
        "ticker": m.get("ticker"),
        "series_ticker": series_ticker,
        "event_ticker": m.get("event_ticker"),
        "title": m.get("title"),
        "status": m.get("status"),
        "result": m.get("result"),
        "open_time": ts(m.get("open_time")),
        "close_time": ts(m.get("close_time")),
        "settlement_ts": ts(m.get("settlement_ts")),  # D7
        "volume": dec(m.get("volume_fp")),
        "open_interest": dec(m.get("open_interest_fp")),
        "price_level_structure": m.get("price_level_structure"),
        "rules_primary": m.get("rules_primary"),
        "raw": jsonb(m),
    }
