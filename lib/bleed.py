"""Taker-bleed aggregation — build-spec §7.

The scoreboard. For every taker fill in a settled market we know what the
aggressor paid and what the contract was worth at settlement, so we know what
they made or lost. Negative total = takers bled = makers earned.

Two invariants worth stating because getting either wrong silently produces a
plausible number:

1. Each trade contributes EXACTLY ONE pnl term, on the taker's side only.
   Every fill has a taker and a maker; counting both sides sums to zero.
2. `taker_side` is the outcome the aggressor bought. `taker_book_side`
   (bid/ask) is book geometry and must never be used here (D12).

Wire values are decimal strings in dollars (D1) and counts are fractional (D2),
so everything is Decimal. Reported units are cents per contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .kalshi import dec

ONE = Decimal(1)
HUNDRED = Decimal(100)
FEE_COEFF = Decimal("0.07")  # Kalshi quadratic fee: 0.07 * C * P * (1-P), D10


@dataclass
class TakerStats:
    trades: int = 0
    contracts: Decimal = Decimal(0)
    taker_yes_contracts: Decimal = Decimal(0)
    taker_no_contracts: Decimal = Decimal(0)
    taker_gross_pnl_cents: Decimal | None = Decimal(0)
    taker_fee_est_cents: Decimal = Decimal(0)
    block_trades_skipped: int = 0
    pages_read: int = 0
    truncated: bool = False
    _yes_notional_cents: Decimal = field(default=Decimal(0), repr=False)
    _no_notional_cents: Decimal = field(default=Decimal(0), repr=False)

    @property
    def taker_yes_vwap_cents(self) -> Decimal | None:
        if not self.taker_yes_contracts:
            return None
        return self._yes_notional_cents / self.taker_yes_contracts

    @property
    def taker_no_vwap_cents(self) -> Decimal | None:
        if not self.taker_no_contracts:
            return None
        return self._no_notional_cents / self.taker_no_contracts

    def as_row(self) -> dict:
        return {
            "trades": self.trades,
            "contracts": self.contracts,
            "taker_yes_contracts": self.taker_yes_contracts,
            "taker_yes_vwap_cents": self.taker_yes_vwap_cents,
            "taker_no_contracts": self.taker_no_contracts,
            "taker_no_vwap_cents": self.taker_no_vwap_cents,
            "taker_gross_pnl_cents": self.taker_gross_pnl_cents,
            "taker_fee_est_cents": self.taker_fee_est_cents,
            "block_trades_skipped": self.block_trades_skipped,
            "pages_read": self.pages_read,
            "truncated": self.truncated,
        }


def trade_prices(trade: dict) -> tuple[Decimal, Decimal]:
    """Return (yes_price, no_price) in dollars.

    Kalshi guarantees yes + no = 1 on a trade; if one side is absent we derive
    it rather than dropping the fill.
    """
    yes = dec(trade.get("yes_price_dollars"))
    no = dec(trade.get("no_price_dollars"))
    if yes is None and no is None:
        raise ValueError(f"trade {trade.get('trade_id')} has no price")
    if yes is None:
        yes = ONE - no
    if no is None:
        no = ONE - yes
    return yes, no


def trade_pnl_cents(trade: dict, result: str) -> Decimal:
    """Taker pnl in cents for one trade, weighted by size. §7.

    taker bought YES at p -> 100*[result==yes] - 100p  per contract
    taker bought NO  at q -> 100*[result==no]  - 100q  per contract
    """
    side = (trade.get("taker_side") or "").lower()
    if side not in ("yes", "no"):
        raise ValueError(f"trade {trade.get('trade_id')} has taker_side={side!r}")

    yes, no = trade_prices(trade)
    count = dec(trade.get("count_fp")) or Decimal(0)
    price = yes if side == "yes" else no
    payoff = ONE if result == side else Decimal(0)
    return (payoff - price) * HUNDRED * count


def trade_fee_cents(trade: dict, fee_multiplier: Decimal | None) -> Decimal:
    """Estimated taker fee in cents, as a POSITIVE magnitude.

    fee/contract = 0.07 * P * (1-P) dollars, scaled by the series' published
    fee_multiplier (D10) — multiplier 0 means the series charges no fee, and
    the flat approximation in §7 would otherwise invent one.

    Known-inaccurate per §7 and left that way for v1: real fees round up
    per-order (so small orders pay proportionally more) and maker fees differ.
    Ranking-grade only.
    """
    mult = ONE if fee_multiplier is None else Decimal(str(fee_multiplier))
    if mult == 0:
        return Decimal(0)
    yes, _ = trade_prices(trade)
    count = dec(trade.get("count_fp")) or Decimal(0)
    return FEE_COEFF * yes * (ONE - yes) * HUNDRED * count * mult


def aggregate(
    trades: Iterable[tuple[dict, int]],
    result: str | None,
    fee_multiplier: Decimal | None = None,
) -> TakerStats:
    """Fold a market's tape into one stats row.

    `trades` is an iterable of (trade, page_index) as produced by
    KalshiClient.iter_trades.

    `result` outside {yes, no} (voids, scalar settles) leaves pnl/fees None —
    the tape is still counted, the PnL is not claimed. §7.
    """
    st = TakerStats()
    scoreable = result in ("yes", "no")
    if not scoreable:
        st.taker_gross_pnl_cents = None
        st.taker_fee_est_cents = None  # type: ignore[assignment]

    for trade, page in trades:
        st.pages_read = max(st.pages_read, page + 1)

        # Defence in depth: we already pass is_block_trade=false (D11), but if
        # the filter is ever dropped these must not enter the metric.
        if trade.get("is_block_trade"):
            st.block_trades_skipped += 1
            continue

        count = dec(trade.get("count_fp")) or Decimal(0)
        side = (trade.get("taker_side") or "").lower()
        yes, no = trade_prices(trade)

        st.trades += 1
        st.contracts += count
        if side == "yes":
            st.taker_yes_contracts += count
            st._yes_notional_cents += yes * HUNDRED * count
        elif side == "no":
            st.taker_no_contracts += count
            st._no_notional_cents += no * HUNDRED * count

        if scoreable:
            st.taker_gross_pnl_cents += trade_pnl_cents(trade, result)  # type: ignore[operator]
            st.taker_fee_est_cents += trade_fee_cents(trade, fee_multiplier)

    return st
