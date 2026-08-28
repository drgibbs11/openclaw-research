"""Hourly temperature markets (KXTEMP*H) — parsing and row mapping.

The product, as verified against the live API rather than assumed:

  * Each market lives exactly one hour. open_time = close_time - 1h for every
    series. There is no day-ahead phase; the whole market is the nowcast hour,
    T-60 to T-0. Extra strikes are added mid-hour as the temperature moves, so
    a ladder's youngest rungs can be minutes old at settlement.
  * close_time IS the target hour: the 1 AM EDT market closes at 05:00Z. The
    :51 routine METAR lands 7-9 minutes before close.
  * Contract encoding: `KXTEMPNYCH-26AUG2801-T76.99` means "77 or above";
    rules_primary reads "above 76.99". So strike_f = ceil(T-value) and YES
    settles iff the reported value exceeds the T-value.

The event code carries the LOCAL hour, not UTC: `-26AUG2801-` is 1 AM local,
which closes at 05:00Z in EDT. Never derive the hour from close_time.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

log = logging.getLogger("hourly")

# The seven hourly temperature series and the station each settles against,
# taken from rules_primary rather than guessed from the city code. CHI is the
# one that punishes guessing: it settles at O'Hare, not Midway.
#
# MIA names no station — its rules say only "Synoptic Data" — and our KMIA feed
# reproduces its settlements 30% of the time against 97-99% elsewhere. It is
# mapped for completeness and excluded from everything downstream.
SERIES_STATION = {
    "KXTEMPNYCH": "KNYC",   # Central Park
    "KXTEMPAUSH": "KAUS",
    "KXTEMPCHIH": "KORD",   # O'Hare, NOT Midway
    "KXTEMPDCH":  "KDCA",
    "KXTEMPLAXH": "KLAX",
    "KXTEMPMIAH": "KMIA",   # nominal only — see above
    "KXTEMPBOSH": "KBOS",
}

HOURLY_SERIES = tuple(SERIES_STATION)

# KORD has no rows in public.observations; it is carried in wethr_nearby_obs.
OBS_FROM_NEARBY = {"KORD"}


def parse_strike(ticker: str) -> Optional[Decimal]:
    """The T-value from a ticker suffix, as quoted. `...-T76.99` -> 76.99.

    Returns None for a ticker with no -T segment rather than raising: the
    hourly series are pure T-ladders today, but a range rung (-B...) would be
    a product change worth skipping past rather than crashing on.
    """
    if "-T" not in ticker:
        return None
    tail = ticker.rsplit("-T", 1)[1]
    try:
        return Decimal(tail)
    except Exception:
        log.warning("unparseable strike in %s", ticker)
        return None


def strike_f(ticker: str) -> Optional[int]:
    """The integer temperature the rung means. ceil(76.99) = 77."""
    s = parse_strike(ticker)
    return None if s is None else int(math.ceil(s))


def event_code(ticker: str) -> Optional[str]:
    """`KXTEMPNYCH-26AUG2801-T76.99` -> `26AUG2801`."""
    parts = ticker.split("-")
    return parts[1] if len(parts) >= 2 else None


# ------------------------------------------------------------------- trades

def trade_row(trade: dict, series_ticker: str | None) -> dict | None:
    """One tape fill -> one market_trades row.

    Prices arrive as decimal dollar strings (D1); we store cents. Kalshi
    guarantees yes + no = 1 on a fill, so a missing yes price is derived rather
    than dropping the row — the same rule lib.bleed applies.
    """
    tid = trade.get("trade_id")
    if not tid:
        return None
    yes = trade.get("yes_price_dollars")
    no = trade.get("no_price_dollars")
    if yes is None and no is None:
        return None
    price = Decimal(str(yes)) if yes is not None else Decimal(1) - Decimal(str(no))

    side = (trade.get("taker_side") or "").lower()
    if side not in ("yes", "no"):
        return None

    ts = trade.get("created_time")
    if not ts:
        return None

    return {
        "kalshi_trade_id": tid,
        "market_ticker": trade.get("ticker"),
        "series_ticker": series_ticker,
        "trade_time": ts,
        "price_cents": price * 100,
        "count": Decimal(str(trade.get("count_fp") or 0)),
        "taker_side": side,
    }


# ------------------------------------------------------------------ candles
# The live and historical candlestick endpoints return DIFFERENT FIELD NAMES
# for the same data (D38), verified on both:
#
#   live  /series/{s}/markets/{t}/candlesticks
#         yes_bid.open_dollars, price.close_dollars, volume_fp, open_interest_fp
#   hist  /historical/markets/{t}/candlesticks
#         yes_bid.open,         price.close,         volume,    open_interest
#
# Reading the historical endpoint with the live field names does not error —
# every price comes back None and the loader writes a full set of NULL rows.
# That is the failure this function exists to prevent, so it accepts either.

_BIDASK_KEYS = ("open", "close", "high", "low")


def _sub(obj: Any, key: str) -> Optional[Decimal]:
    """Read `key` from a candle sub-object under either naming convention."""
    if not isinstance(obj, dict):
        return None
    v = obj.get(f"{key}_dollars")
    if v is None:
        v = obj.get(key)
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v)) * 100
    except Exception:
        return None


def _count(candle: dict, base: str) -> Optional[Decimal]:
    v = candle.get(f"{base}_fp")
    if v is None:
        v = candle.get(base)
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def candle_row(candle: dict, market_ticker: str) -> dict | None:
    """One candlestick -> one market_candles_1m row, from either endpoint."""
    ts = candle.get("end_period_ts")
    if ts is None:
        return None
    bucket = datetime.fromtimestamp(int(ts), tz=timezone.utc)

    bid, ask, price = candle.get("yes_bid"), candle.get("yes_ask"), candle.get("price")
    return {
        "market_ticker": market_ticker,
        "bucket_time": bucket,
        "yes_bid_open_cents": _sub(bid, "open"),
        "yes_bid_close_cents": _sub(bid, "close"),
        "yes_ask_open_cents": _sub(ask, "open"),
        "yes_ask_close_cents": _sub(ask, "close"),
        "price_open_cents": _sub(price, "open"),
        "price_high_cents": _sub(price, "high"),
        "price_low_cents": _sub(price, "low"),
        "price_close_cents": _sub(price, "close"),
        "volume": _count(candle, "volume"),
        "open_interest": _count(candle, "open_interest"),
    }


def is_change_row(row: dict, prev: dict | None) -> bool:
    """Keep a minute only if it traded or the touch moved.

    The API is already sparse — a 60-minute market came back with 26 candles —
    but sparse is not the same as change-only: it returns quiet minutes whose
    quotes are unchanged. This is the second filter, applied on top.

    The FIRST row of a market is always kept whatever it says: it is the T-60
    opening book, the row market_snapshots is reconciled against (CP5), and
    dropping it because the book happened to open unchanged from nothing would
    remove the one minute the ladder view depends on.
    """
    if prev is None:
        return True
    if (row.get("volume") or 0) > 0:
        return True
    return (row.get("yes_bid_close_cents") != prev.get("yes_bid_close_cents")
            or row.get("yes_ask_close_cents") != prev.get("yes_ask_close_cents"))
