"""Offline smoke test — exercises the client and mappers against live API data
without touching Postgres. Run before deploying to catch field-name drift.

Usage: python tools/smoke_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from lib import mappers  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


def main() -> int:
    client = KalshiClient()
    now = datetime.now(timezone.utc)

    print("series")
    series = client.list_series()
    check("GET /series returns rows", len(series) > 1000, f"{len(series)} series")
    srow = mappers.series_row(series[0], now)
    check("series_row has a primary key", bool(srow["series_ticker"]))
    check("frequency populated", bool(srow["frequency"]), str(srow["frequency"]))

    print("events")
    ev = next(client.iter_events(), None)
    check("events reachable", ev is not None)
    if ev:
        erow = mappers.event_row(ev, now)
        check("event carries series_ticker (D14)", bool(erow["series_ticker"]),
              str(erow["series_ticker"]))

    print("open markets")
    m = next(client.iter_markets(status="open"), None)
    check("open market reachable", m is not None)
    if m:
        check("MVE excluded (D8)", "mve_collection_ticker" not in m or not m.get("mve_collection_ticker"),
              m.get("ticker", ""))
        snap = mappers.snapshot_row(m, now, None)
        check("prices parse as numbers (D1)",
              snap["yes_ask"] is None or 0 <= float(snap["yes_ask"]) <= 1,
              f"yes_ask={snap['yes_ask']}")
        check("volume parses (D2)", snap["volume"] is not None, f"volume={snap['volume']}")
        check("close_time parses to UTC (G5)",
              snap["close_time"] is None or snap["close_time"].tzinfo is not None)

    print("settled markets")
    # 400, not 40: ~76% of settled markets never trade (D23) and they arrive in
    # runs, so a 40-market sample is regularly all-zero-volume and the tape
    # check below finds nothing to score. That is a flaky test, not a finding.
    settled = [x for _, x in zip(range(400), client.iter_markets(status="settled"))]
    check("settled markets reachable", len(settled) > 0, f"{len(settled)} fetched")
    terminal = [x for x in settled if x.get("status") in mappers.TERMINAL_STATUSES]
    check("payload status is terminal (D4)", len(terminal) == len(settled),
          f"statuses={sorted({x.get('status') for x in settled})}")
    if terminal:
        trow = mappers.terminal_row(terminal[0], None)
        check("settlement_ts present (D7)", trow["settlement_ts"] is not None,
              str(trow["settlement_ts"]))

    print("tape")
    scored = next((x for x in settled
                   if x.get("result") in ("yes", "no") and float(x.get("volume_fp") or 0) > 10), None)
    check("found a scoreable settled market", scored is not None)
    if scored:
        trades = list(client.iter_trades(scored["ticker"], max_pages=2))
        check("tape returns trades", len(trades) > 0, f"{len(trades)} trades")
        if trades:
            t = trades[0][0]
            check("taker_side present (D12)", t.get("taker_side") in ("yes", "no"),
                  str(t.get("taker_side")))
            check("count_fp present (D2)", t.get("count_fp") is not None)
            check("block trades filtered (D11)", not t.get("is_block_trade"))

    print(f"\n{'PASS' if not FAILS else 'FAIL: ' + ', '.join(FAILS)} "
          f"({client.request_count} requests)")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
