"""Job D — classify series into series_tags. One-time + top-up, no API key.

Originally an LLM call per series. It is now a deterministic rules pass over
the series payload (lib/classify_rules.py). The output schema is unchanged, so
v_screen and v_screen_funnel are untouched — only the source of the judgment
moved from a model to an auditable rules table.

Why rules: measured over all 12,564 live series, three of the four fields are
mechanical (D28). `recurrence` reads straight off `series.frequency` (D9),
`settlement_source_type` and `scrape_difficulty` are functions of the
settlement URL's domain, and 99.94% of series publish one. Only `benchmark` is
a judgment call, and it collapses to category + domain rules.

Rerunning is free and idempotent, so this is safe on a cron. Rows already
marked `reviewed = true` are never overwritten — a human decision outranks the
rules.

  python jobs/classify.py              # tag anything untagged
  python jobs/classify.py --all        # include series with no settled history
  python jobs/classify.py --recheck    # re-apply rules to unreviewed rows too
"""

from __future__ import annotations

import argparse
import collections
import logging
import sys

sys.path.insert(0, ".")

from lib import classify_rules, db  # noqa: E402

log = logging.getLogger("classify")


def candidates(conn, classify_all: bool, recheck: bool) -> list[dict]:
    """Series needing a tag.

    v_screen requires recurrence='recurring', so one_off series can never
    surface and are skipped outright (§8 permits this). Without --all we also
    require settled history, since the bleed half of the screen needs it.
    """
    history = "" if classify_all else """
          and exists (select 1 from markets_terminal mt
                      where mt.series_ticker = s.series_ticker)
    """
    # A reviewed row is a human decision and always wins over the rules.
    untagged = "and t.series_ticker is null" if not recheck else \
               "and (t.series_ticker is null or t.reviewed is not true)"
    sql = f"""
        select s.series_ticker, s.title, s.category, s.frequency,
               s.settlement_sources
        from series s
        left join series_tags t using (series_ticker)
        where coalesce(s.frequency, '') <> 'one_off'
          {untagged}
          {history}
        order by s.series_ticker
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="classify recurring series with no settled history too")
    ap.add_argument("--recheck", action="store_true",
                    help="re-apply rules to existing unreviewed rows")
    ap.add_argument("--limit", type=int, help="stop after N series")
    args = ap.parse_args()

    with db.connect() as conn:
        rows = candidates(conn, args.all, args.recheck)
        if args.limit:
            rows = rows[:args.limit]
        log.info("classifying %d series with %s", len(rows),
                 classify_rules.RULES_VERSION)

        tally: collections.Counter = collections.Counter()
        batch = []
        for r in rows:
            tag = classify_rules.classify(r)
            tally[(tag["settlement_source_type"], tag["benchmark"],
                   tag["recurrence"])] += 1
            batch.append({
                "series_ticker": r["series_ticker"],
                **tag,
                "source_urls": db.jsonb(tag["source_urls"]),
            })

        if batch:
            db.upsert(conn, "series_tags", batch, conflict="series_ticker")
            conn.commit()

    log.info("wrote %d tags", len(batch))
    for (src, bench, rec), n in tally.most_common(12):
        log.info("  %5d  %-24s %-20s %s", n, src, bench, rec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
