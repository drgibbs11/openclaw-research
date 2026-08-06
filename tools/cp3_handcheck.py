"""CP3 hand-check — build-spec §7/§10.

Pulls real settled tapes and verifies lib/bleed against an INDEPENDENT
arithmetic path written from the spec text rather than from the library, so a
bug in the library cannot make itself pass. Prints a small market trade by
trade so a human can check the arithmetic by eye.

Usage: python -m tools.cp3_handcheck [--n 3]
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

sys.path.insert(0, ".")

from lib.bleed import aggregate  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

PAGE_CAP = 50


def independent_pnl(trades: list[dict], result: str) -> tuple[Decimal, Decimal, Decimal]:
    """Recomputed straight from §7's prose. Deliberately not sharing code with
    lib.bleed — different loop, different structure.

    'taker bought YES at yes_price p: pnl/contract = 100*[R=yes] - p cents'
    'taker bought NO at no_price q:   pnl/contract = 100*[R=no]  - q cents'
    """
    pnl = Decimal(0)
    contracts = Decimal(0)
    fees = Decimal(0)
    for t in trades:
        n = Decimal(t["count_fp"])
        p_yes_cents = Decimal(t["yes_price_dollars"]) * 100
        p_no_cents = Decimal(t["no_price_dollars"]) * 100
        if t["taker_side"] == "yes":
            per = (Decimal(100) if result == "yes" else Decimal(0)) - p_yes_cents
        else:
            per = (Decimal(100) if result == "no" else Decimal(0)) - p_no_cents
        pnl += per * n
        contracts += n
        # 7 * p_hat * (1 - p_hat) cents, p_hat in [0,1]
        ph = Decimal(t["yes_price_dollars"])
        fees += Decimal(7) * ph * (Decimal(1) - ph) * n
    return pnl, contracts, fees


def pick_markets(client: KalshiClient, n: int) -> list[dict]:
    """Settled, non-MVE, yes/no result, with a tape small enough to eyeball."""
    out = []
    for m in client.iter_markets(status="settled", max_pages=6):
        vol = Decimal(m.get("volume_fp") or 0)
        if m.get("result") in ("yes", "no") and 20 < vol < 4000:
            out.append(m)
            if len(out) >= n:
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    args = ap.parse_args()

    client = KalshiClient()
    markets = pick_markets(client, args.n)
    if len(markets) < args.n:
        print(f"only found {len(markets)} suitable markets", file=sys.stderr)

    all_ok = True
    for i, m in enumerate(markets):
        ticker, result = m["ticker"], m["result"]
        raw = list(client.iter_trades(ticker, max_pages=PAGE_CAP))
        trades = [t for t, _ in raw]

        st = aggregate(iter(raw), result, fee_multiplier=Decimal(1))
        exp_pnl, exp_ct, exp_fee = independent_pnl(trades, result)

        ok = (
            st.taker_gross_pnl_cents == exp_pnl
            and st.contracts == exp_ct
            and st.taker_fee_est_cents == exp_fee
        )
        all_ok &= ok

        print(f"\n{'='*74}\n[{i+1}] {ticker}   result={result}   trades={len(trades)}")
        print(f"    volume_fp (market) = {m.get('volume_fp')}   tape contracts = {st.contracts}")
        print(f"    {'lib':>14} {'independent':>16}")
        print(f"    pnl_cents  {str(st.taker_gross_pnl_cents):>14} {str(exp_pnl):>16}")
        print(f"    contracts  {str(st.contracts):>14} {str(exp_ct):>16}")
        print(f"    fee_cents  {str(st.taker_fee_est_cents):>14} {str(exp_fee):>16}")
        print(f"    -> {'MATCH' if ok else 'MISMATCH'}")
        if st.contracts:
            print(f"    bleed = {st.taker_gross_pnl_cents / st.contracts:.4f} cents/contract "
                  f"({'takers bled' if st.taker_gross_pnl_cents < 0 else 'takers won'})")

        # Eyeball table for the smallest market.
        if len(trades) <= 12:
            print("    per-trade:")
            for t in trades:
                side = t["taker_side"]
                px = t[f"{side}_price_dollars"]
                n = t["count_fp"]
                won = result == side
                per = (100 if won else 0) - Decimal(px) * 100
                print(f"      taker bought {side.upper():<3} {n:>9} @ ${px}  "
                      f"settled {result.upper():<3} -> {per:>8.2f} c/ct  "
                      f"= {per * Decimal(n):>12.2f} c")

    print(f"\n{'='*74}\nCP3: {'PASS' if all_ok else 'FAIL'} "
          f"({len(markets)} markets, {client.request_count} requests)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
