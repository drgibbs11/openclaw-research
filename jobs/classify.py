"""Job D — classification. One-time + weekly top-up. (build-spec §8)

One Anthropic Messages API call per series. Resumable: skips series already in
series_tags.

Four deliberate deviations from §8, each earning its place:

1. SCOPE. §8 assumed "a few hundred" series; there are 12,525 (D5). Classifying
   all of them is ~12.5k API calls to populate a table whose only consumer is
   v_screen. We classify only series that could plausibly surface there:
   recurring frequency, and (unless --all) some settled-market history. That is
   a few hundred calls, which is what §8 budgeted for.

2. RECURRENCE IS DERIVED, NOT ASKED. series.frequency is a populated enum
   (D9), so asking a model to infer it is strictly worse. Only `custom`
   frequency series get the question.

3. THE BENCHMARK CONTRADICTION. §8's prompt says "Use ONLY the provided text"
   and then asks whether a liquid professional market prices the same outcome —
   which the text never states. As written the model must answer "unknown" (and
   `where benchmark = 'none'` returns no rows) or guess. The prompt below scopes
   the text-only rule to the fields it belongs to and lets the model use general
   knowledge for `benchmark` alone, with an explicit uncertainty instruction.

4. NO `temperature`. §8 says temperature 0; current Claude models reject
   non-default sampling parameters with a 400. Determinism now comes from the
   task shape and a JSON schema, not a sampling knob.

Also: §8's "output ONLY a JSON object, no markdown fences" is what structured
outputs are for. The schema is enforced by the API rather than requested in
prose, so there is no fence-stripping or repair path to get wrong.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Literal

sys.path.insert(0, ".")

import anthropic  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from lib import db  # noqa: E402
from lib.mappers import recurrence_from_frequency  # noqa: E402

log = logging.getLogger("classify")

# §8 says to pin a current model. The doc's example (claude-sonnet-4-6) predates
# the Claude 5 family; claude-sonnet-5 is the current Sonnet-tier ID and keeps
# the spec's cost intent for a few-hundred-call classification pass.
MODEL = os.environ.get("CLASSIFY_MODEL", "claude-sonnet-5")
RULES_TRUNCATE = 4000  # §8


class SeriesTag(BaseModel):
    settlement_source_type: Literal[
        "scrapable_numeric_feed", "official_report",
        "committee_or_subjective", "market_price", "unknown",
    ]
    benchmark: Literal[
        "none", "sportsbook", "cme_or_rates", "spot_crypto",
        "other_liquid_market", "unknown",
    ]
    benchmark_name: str | None
    recurrence: Literal["recurring", "one_off", "unknown"]
    scrape_difficulty: Literal["low", "medium", "high", "unknown"]
    source_urls: list[str]
    # No max_length constraint: the API's schema dialect drops it, so it would
    # be enforced client-side and turn a slightly-long note into a hard
    # validation failure. The prompt asks for <=200 chars; we truncate on write.
    notes: str = Field(default="")


SYSTEM = """\
You are classifying a prediction-market series for a screening database.

Two of these fields are read off the provided text and two are judgment calls.
Keep them separate.

FROM THE TEXT ONLY — do not use outside knowledge of current events, and do not
infer beyond what the settlement sources and rules state:

- settlement_source_type: how this market resolves.
  "scrapable_numeric_feed" — settles to a number published on a machine-readable
    or reliably scrapable public page (government data series, exchange prints,
    published statistics).
  "official_report" — settles to an official publication that is public but
    irregular or document-shaped.
  "committee_or_subjective" — human judgment, awards, review aggregation with
    editorial discretion.
  "market_price" — settles to the price of a traded instrument.
  "unknown" — the text does not determine it.

- source_urls: URLs that literally appear in the provided settlement_sources or
  rules text. Never invent a URL. Empty list if there are none.

USING YOUR GENERAL KNOWLEDGE — the text will not answer these, and answering
"unknown" because the text is silent is wrong:

- benchmark: does a liquid professional market already price essentially this
  same outcome? "sportsbook", "cme_or_rates", "spot_crypto",
  "other_liquid_market", or "none" if you are confident no such market exists.
  Use "unknown" only when you genuinely cannot judge — not merely because the
  provided text does not mention a benchmark.
- benchmark_name: name the benchmark, or null when benchmark is "none".
- scrape_difficulty: effort to automate reading the settlement source on a
  schedule. "low" | "medium" | "high" | "unknown".

- recurrence: use the value supplied in the input if one is given. It is
  derived from the exchange's own frequency field and is authoritative. Judge it
  yourself only when the input says "unknown".

- notes: at most 200 characters.
"""


def build_payload(row: dict) -> dict:
    payload = {
        "series_ticker": row["series_ticker"],
        "title": row["title"],
        "category": row["category"],
        "frequency": row["frequency"],
        "tags": row["tags"],
        "settlement_sources": row["settlement_sources"],
        "recurrence": row["derived_recurrence"],  # D9
    }
    if row.get("rules_primary"):
        payload["rules_primary"] = row["rules_primary"][:RULES_TRUNCATE]
    return payload


def candidates(conn, classify_all: bool) -> list[dict]:
    """Series worth spending a call on — see deviation 1.

    v_screen requires recurrence='recurring', so one_off series can never
    surface and are skipped outright (§8 permits this). Without --all we also
    require settled history, since the bleed half of the screen needs it.
    """
    history = "" if classify_all else """
          and exists (select 1 from markets_terminal mt
                      where mt.series_ticker = s.series_ticker)
    """
    sql = f"""
        select s.series_ticker, s.title, s.category, s.frequency,
               s.tags, s.settlement_sources,
               (select mt.rules_primary from markets_terminal mt
                 where mt.series_ticker = s.series_ticker
                   and mt.rules_primary is not null and mt.rules_primary <> ''
                 order by mt.settlement_ts desc nulls last limit 1) as rules_primary
        from series s
        left join series_tags t using (series_ticker)
        where t.series_ticker is null
          and coalesce(s.frequency, '') <> 'one_off'
          {history}
        order by s.series_ticker
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        r["derived_recurrence"] = recurrence_from_frequency(r["frequency"])
    return rows


def classify_one(client: anthropic.Anthropic, row: dict) -> SeriesTag:
    """One call. No `temperature` — rejected on current models (see module doc).

    Thinking is disabled and effort is low: this is a bounded classification
    over a few thousand characters, which is exactly the workload those settings
    are for.
    """
    response = client.messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        thinking={"type": "disabled"},
        # The SDK turns `output_format` into a JSON schema and merges it into
        # output_config as `format`. Passing the model class inside
        # output_config directly skips that transform and leaves
        # parsed_output empty — pass them as separate arguments.
        output_format=SeriesTag,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": json.dumps(build_payload(row), indent=2, default=str),
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"refused: {row['series_ticker']}")
    return response.parsed_output


def store(conn, series_ticker: str, tag: SeriesTag, derived_recurrence: str) -> None:
    # D9: trust the exchange's frequency over the model wherever it is decisive.
    recurrence = tag.recurrence if derived_recurrence == "unknown" else derived_recurrence
    db.upsert(conn, "series_tags", [{
        "series_ticker": series_ticker,
        "settlement_source_type": tag.settlement_source_type,
        "benchmark": tag.benchmark,
        "benchmark_name": tag.benchmark_name,
        "recurrence": recurrence,
        "scrape_difficulty": tag.scrape_difficulty,
        "source_urls": db.jsonb(tag.source_urls),
        "notes": (tag.notes or "")[:200],
        "model": MODEL,
        "reviewed": False,
    }], conflict="series_ticker")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="classify recurring series with no settled history too")
    ap.add_argument("--limit", type=int, help="stop after N series")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY is not set")
        return 1

    client = anthropic.Anthropic()

    with db.connect() as conn:
        rows = candidates(conn, args.all)
        if args.limit:
            rows = rows[:args.limit]
        log.info("classifying %d series with %s", len(rows), MODEL)

        done = failed = 0
        for i, row in enumerate(rows, 1):
            try:
                tag = classify_one(client, row)
                store(conn, row["series_ticker"], tag, row["derived_recurrence"])
                done += 1
            except Exception as exc:
                failed += 1
                log.warning("failed %s: %s", row["series_ticker"], exc)
            if i % 25 == 0:
                conn.commit()
                log.info("%d/%d (%d failed)", i, len(rows), failed)
        conn.commit()

    log.info("done: %d classified, %d failed", done, failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
