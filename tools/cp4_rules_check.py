"""CP4 gate — verifies the deterministic classifier against the live API.

Needs no database and no API key. Fetches /series once and checks the rules in
lib/classify_rules.py against known-answer cases, then reports the tag
distribution so a regression in coverage is visible.

The headline check is CP5 ground truth: the real temperature series must come
back scrapable_numeric_feed + benchmark=none + recurring. Note the scope — it
is constrained to category='Climate and Weather' on purpose, because the ticker
prefix alone also matches Lowe's (KXLOW, KXLOWA, KXLOWCC) and "Highest margin
of victory" (KXHIGHMOV*). See D30.

Usage: python tools/cp4_rules_check.py
"""

from __future__ import annotations

import collections
import sys

sys.path.insert(0, ".")

from lib import classify_rules as cr  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

FAILS: list[str] = []

# (ticker, field, expected) — hand-checked against the live payloads.
KNOWN = [
    ("KXHIGHNY", "settlement_source_type", "scrapable_numeric_feed"),
    ("KXHIGHNY", "benchmark", "none"),
    ("KXHIGHNY", "recurrence", "recurring"),
    # Filed under category 'World', not 'Climate and Weather' — the settlement
    # domain has to win over the category label (D30).
    ("KXHIGHNY0", "benchmark", "none"),
    # Subdomain: wpc.ncep.noaa.gov must resolve to noaa.gov (D30).
    ("KXHIGHUS", "settlement_source_type", "scrapable_numeric_feed"),
    # Crypto has a sharp external mark, so it must not read as `none`.
    ("KXETHY", "benchmark", "spot_crypto"),
    # `custom` is ambiguous by design and must not be guessed (D9).
    ("KXHIGHTEMPDEN", "recurrence", "unknown"),
]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


def main() -> int:
    client = KalshiClient()
    series = {s["ticker"]: s for s in client.list_series()}
    print(f"series: {len(series)}\n")

    print("known answers")
    for ticker, field, expected in KNOWN:
        s = series.get(ticker)
        if s is None:
            check(f"{ticker}.{field}", False, "series not listed")
            continue
        got = cr.classify(s)[field]
        check(f"{ticker}.{field}", got == expected, f"got {got!r}, want {expected!r}")

    print("\nCP5 ground truth (temperature series only — see D30)")
    temp = [s for t, s in series.items()
            if (t.startswith("KXHIGH") or t.startswith("KXLOW"))
            and s.get("category") == "Climate and Weather"]
    passes = [s for s in temp
              if cr.settlement_source_type(s) == "scrapable_numeric_feed"
              and cr.benchmark(s)[0] == "none"
              and cr.recurrence(s) == "recurring"]
    # 52/53 at time of writing; the residual is KXHIGHTEMPDEN (frequency
    # `custom`). Allow one, fail on a real regression.
    check("temperature series classify correctly",
          len(temp) - len(passes) <= 1, f"{len(passes)}/{len(temp)} pass")
    for s in temp:
        if s not in passes:
            print(f"        residual {s['ticker']} ({s['frequency']})")

    print("\ncoverage")
    tally = collections.Counter(cr.settlement_source_type(s) for s in series.values())
    for k, v in tally.most_common():
        print(f"    {v:6d}  {k}")
    # A high `unknown` count is expected and fine: ~5,600 of these series are
    # Sports/Entertainment settling to media sites, which are deliberately not
    # in the allowlist and are screened out anyway. What must not regress is
    # coverage of the sources the screen actually depends on.
    usable = tally["scrapable_numeric_feed"] + tally["official_report"]
    check("screenable sources still resolve", usable >= 1500,
          f"{usable} scrapable_numeric_feed + official_report")

    print(f"\n{'PASS' if not FAILS else 'FAIL: ' + ', '.join(FAILS)} "
          f"({client.request_count} requests)")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
