"""Deterministic series classification — the rules that replace the LLM call.

Job D originally asked a model to fill in `series_tags`. It doesn't need to.
Measured against all 12,564 live series (see DISCREPANCIES D28):

  - `recurrence` was never a judgment call. `series.frequency` is a populated
    enum, so it is read straight off the exchange (D9). Only `custom` is
    genuinely ambiguous and it stays `unknown` rather than being guessed.
  - `settlement_source_type` is a function of the settlement URL's domain.
    99.94% of series publish one.
  - `scrape_difficulty` follows from the same domain.
  - `benchmark` is the only real judgment call, and it collapses to a small
    set of category + domain rules.

Rules beat a model here: they are auditable, free, reproducible run to run, and
they cannot hallucinate a settlement source that isn't in the payload. The cost
is coarseness on `benchmark`, which is acceptable because the surviving
candidate set is small enough to eyeball — `series_tags.reviewed` exists for
exactly that, and anything the rules can't place lands in `unknown` rather than
being quietly assigned.

CP5 ground truth: 52/53 real temperature series classify correctly. The single
residual is KXHIGHTEMPDEN, whose frequency is `custom`.
"""

from __future__ import annotations

from urllib.parse import urlparse

RULES_VERSION = "rules:v1"

# --- settlement_source_type ------------------------------------------------
# Structured numeric data you can poll and parse without a human in the loop.
NUMERIC = {
    "weather.gov", "noaa.gov", "weather.com", "cfbenchmarks.com",
    "fred.stlouisfed.org", "tradingeconomics.com", "coinmarketcap.com",
    "coingecko.com", "eia.gov",
}
# Scheduled institutional releases — parseable, but on the publisher's calendar.
OFFICIAL = {
    "bls.gov", "bea.gov", "census.gov", "treasury.gov", "federalreserve.gov",
    "nber.org", "cdc.gov", "usda.gov", "dot.gov", "bts.gov", "faa.gov",
    "sec.gov", "irs.gov", "whitehouse.gov",
}
# A price feed is a benchmark, not a source to scrape against.
MARKET = {
    "cmegroup.com", "nasdaq.com", "nyse.com", "tradingview.com",
    "marketwatch.com", "coinbase.com", "binance.com", "kraken.com",
}
# Human/committee decisions — no numeric feed exists at all.
SUBJECTIVE = {
    "oscars.org", "theamas.com", "nobelprize.org", "emmys.com", "grammy.com",
    "billboard.com", "luminatedata.com", "rottentomatoes.com", "metacritic.com",
}

# --- scrape_difficulty -----------------------------------------------------
DIFFICULTY = {
    "weather.gov": "low", "weather.com": "low", "cfbenchmarks.com": "low",
    "fred.stlouisfed.org": "low", "coinmarketcap.com": "low",
    "coingecko.com": "low", "noaa.gov": "medium", "tradingeconomics.com": "medium",
    "bls.gov": "medium", "bea.gov": "medium", "census.gov": "medium",
    "eia.gov": "medium", "federalreserve.gov": "medium", "bts.gov": "medium",
    "nber.org": "high",
}

# Weather settles to an observation nobody trades a sharp book against. This is
# checked before category because the category label is unreliable — KXHIGHNY0
# ("NYC high temperature") is filed under `World`, not `Climate and Weather`.
NO_BENCHMARK_DOMAINS = {"weather.gov", "noaa.gov", "weather.com"}
CRYPTO_DOMAINS = {"cfbenchmarks.com", "coinmarketcap.com", "coingecko.com"}
RATES_DOMAINS = {"federalreserve.gov", "nber.org"}

RECURRING_FREQUENCIES = {
    "fifteen_min", "hourly", "daily", "weekly", "monthly", "quarterly", "annual",
}


def domains(series: dict) -> set[str]:
    return {
        urlparse((s.get("url") or "").strip()).netloc.lower().replace("www.", "")
        for s in (series.get("settlement_sources") or [])
        if (s.get("url") or "").strip()
    }


def hits(series: dict, group: set[str]) -> set[str]:
    """Suffix match, so wpc.ncep.noaa.gov resolves to noaa.gov.

    Exact set membership silently misdirected every NOAA subdomain to
    `unknown` — see D30.
    """
    d = domains(series)
    return {x for x in d if any(x == g or x.endswith("." + g) for g in group)}


def source_urls(series: dict) -> list[str]:
    return [
        (s.get("url") or "").strip()
        for s in (series.get("settlement_sources") or [])
        if (s.get("url") or "").strip()
    ]


def settlement_source_type(series: dict) -> str:
    if hits(series, NUMERIC):
        return "scrapable_numeric_feed"
    if hits(series, OFFICIAL):
        return "official_report"
    if hits(series, MARKET):
        return "market_price"
    if hits(series, SUBJECTIVE):
        return "committee_or_subjective"
    return "unknown"


def benchmark(series: dict) -> tuple[str, str | None]:
    """Returns (benchmark, benchmark_name).

    `none` is the interesting answer: no sharp external reference means a
    passive seat isn't being picked off by somebody with a faster mark.
    """
    category = series.get("category") or ""
    if hits(series, NO_BENCHMARK_DOMAINS):
        return "none", None
    if category == "Sports":
        return "sportsbook", "retail sportsbooks"
    if category == "Crypto" or hits(series, CRYPTO_DOMAINS):
        return "spot_crypto", "spot exchanges"
    if category == "Financials":
        return "cme_or_rates", "CME / rates futures"
    if category in ("Politics", "Elections"):
        # These are the markets Polymarket lists too, so there is a liquid
        # external book to be marked against.
        return "other_liquid_market", "Polymarket"
    if category == "Climate and Weather":
        return "none", None
    if category == "Economics":
        if hits(series, RATES_DOMAINS):
            return "cme_or_rates", "fed funds futures"
        return "none", None
    return "unknown", None


def scrape_difficulty(series: dict) -> str:
    for d in domains(series):
        for group, level in DIFFICULTY.items():
            if d == group or d.endswith("." + group):
                return level
    return "unknown"


def recurrence(series: dict) -> str:
    """D9: structural, never inferred. `custom` stays unknown by design."""
    f = (series.get("frequency") or "").lower()
    if f == "one_off":
        return "one_off"
    return "recurring" if f in RECURRING_FREQUENCIES else "unknown"


def classify(series: dict) -> dict:
    """Full tag row for one series. Pure function of the series payload."""
    bench, bench_name = benchmark(series)
    src = settlement_source_type(series)
    note = f"{src}/{bench}"
    doms = sorted(domains(series))
    if doms:
        note += " via " + ",".join(doms[:3])
    return {
        "settlement_source_type": src,
        "benchmark": bench,
        "benchmark_name": bench_name,
        "recurrence": recurrence(series),
        "scrape_difficulty": scrape_difficulty(series),
        "source_urls": source_urls(series),
        "notes": note[:200],
        "model": RULES_VERSION,
        "reviewed": False,
    }
