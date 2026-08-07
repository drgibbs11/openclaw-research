# Kalshi Long-Tail Screener

A screening instrument, not a trading system. It answers one question:

> Which recurring Kalshi series settle to a scrapable public source, have no
> sharp external benchmark, sit in a solo-fillable volume band, and show
> historical taker bleed?

Output is a ranked SQL view, `v_screen`. The top rows nominate the 2–3 series
worth modeling next. **No order placement anywhere.** All market data comes from
public, unauthenticated `GET` endpoints.

Stack: Railway (Python cron workers) + Supabase (Postgres).

---

## Layout

```
spec/openapi.yaml       vendored Kalshi event-contract OpenAPI spec
fixtures/*.json         captured live responses, verbatim
DISCREPANCIES.md        every place the build spec and the live API disagree
sql/001_schema.sql      DDL
sql/010_views.sql       v_series_activity, v_taker_bleed, v_screen + diagnostics
sql/020_prune.sql       snapshot retention — run daily
lib/kalshi.py           paginated GET client — throttle, backoff, cursor paging
lib/bleed.py            the taker-bleed metric
lib/tape.py             shared tape aggregation for Jobs B and C
lib/mappers.py          API object -> DB row
lib/db.py               psycopg3 helpers, idempotent upserts
jobs/snapshot.py        Job A — every 6h
jobs/settled_sweep.py   Job B — daily
jobs/backfill.py        Job C — one-time, resumable
jobs/classify.py        Job D — one-time + weekly top-up
tools/cp3_handcheck.py  CP3 gate: verifies the PnL math against real tapes
tools/smoke_test.py     offline check of client + mappers against the live API
```

## Setup

```bash
pip install -r requirements.txt
export DATABASE_URL=...        # Supabase
export ANTHROPIC_API_KEY=...   # Job D only
psql "$DATABASE_URL" -f sql/001_schema.sql
psql "$DATABASE_URL" -f sql/010_views.sql
```

Everything is created in a dedicated **`screener` schema**, never `public`.
The target Supabase project is a live weather-trading system with 80 public
tables — one of which is already called `market_snapshots`, with an unrelated
shape. Under `create table if not exists` that collision is silent: the create
is skipped and the first insert fails on columns that don't exist. `lib/db.py`
sets `search_path` on every connection and refuses to run if the schema is
missing, so application code stays unqualified but can never touch `public`.
Override with `SCREENER_SCHEMA` if you relocate it.

**Supabase pooler gotcha:** if `DATABASE_URL` is the transaction pooler
(port 6543), psycopg3 prepared statements break. `lib/db.py` passes
`prepare_threshold=None` unconditionally, which is correct on the pooler and
harmless on a direct connection.

| Env var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | Supabase Postgres |
| `ANTHROPIC_API_KEY` | — | Job D only |
| `KALSHI_BASE_URL` | `https://api.elections.kalshi.com/trade-api/v2` | API base |
| `THROTTLE_RPS` | `5` | Client-side request rate |
| `TAPE_PAGE_CAP` | `50` | Pages per market on the tape |
| `TAPE_MIN_VOLUME` | `1` | Skip zero-volume markets (empty tapes), Jobs B and C |
| `BACKFILL_DAYS` | `180` | Backfill window |
| `BACKFILL_MIN_VOLUME` | `TAPE_MIN_VOLUME` | Per-job override of the floor |
| `BACKFILL_SOURCE` | `archive` | `archive` (deep, per-series) or `recent` (live only) |
| `BACKFILL_EXCLUDE_CATEGORIES` | — | Comma-separated, e.g. `Sports` |
| `BACKFILL_SKIP_INGEST` | — | Resume straight into phase 2 |
| `TERMINAL_MIN_VOLUME` | `1` | Skip never-traded settled markets; `0` keeps everything (see Storage) |
| `SCREENER_SCHEMA` | `screener` | Postgres schema; never `public` |
| `CLASSIFY_MODEL` | `claude-sonnet-5` | Job D model |

## Running

```bash
python jobs/snapshot.py        # Job A — cron: 0 */6 * * *
python jobs/settled_sweep.py   # Job B — cron: 30 6 * * *
python jobs/backfill.py        # Job C — manual, resumable
python jobs/classify.py        # Job D — manual, resumable
```

### Job C reads the archive, not the live endpoint

`GET /markets` is a **rolling recent window**, not the full record — it reaches
back roughly 75 days on the series measured, and returns *nothing at all* for
slower ones. `min_settled_ts` beyond that window is silently ignored rather than
refused. `/historical/markets` holds the real depth:

| series | `/markets` | oldest | `/historical/markets` | oldest |
|---|---:|---|---:|---|
| KXHIGHNY | 444 | 2026-05-25 | 8,956 | 2021-08-08 |
| KXCPIYOY | 48 | 2026-06-10 | 555 | 2022-12-13 |
| KXJOBLESS | **0** | — | **71** | 2021-08-14 |

This is retention, not authentication — see D26. Everything is public; no API
key is needed. But it means Job C walks the archive per recurring series by
default. Set `BACKFILL_SOURCE=recent` to use only the cheap live walk, at the
cost of depth.

The tape splits the same way: an archived market returns zero trades from
`/markets/trades` and its real tape from `/historical/trades`. `lib/tape.py`
falls back automatically.

### Size Job C before you start it

Kalshi settles roughly **70,000 markets a day** — mostly auto-generated strike
ladders on high-frequency crypto series, ~76% of which never trade (D23). A
literal 180-day backfill therefore spans on the order of 12M markets. Ingest is
cheap; a tape request per market is not — even limited to the ~24% with volume,
that is millions of requests, i.e. days of continuous paging at 5 req/s.

Job C is resumable, so a multi-day run is viable. If you'd rather bound it:

```bash
BACKFILL_DAYS=30 BACKFILL_MIN_VOLUME=100 python jobs/backfill.py
BACKFILL_EXCLUDE_CATEGORIES=Sports,Entertainment python jobs/backfill.py
```

Note the interaction with `v_screen`'s `n_settled >= 20` threshold: a shorter
window means fewer settled markets per series, so tune the two together.

Verification, neither of which needs a database:

```bash
python tools/smoke_test.py     # client + mappers vs. the live API
python tools/cp3_handcheck.py  # CP3 gate — PnL math vs. real tapes
```

## Deployment status

The `screener` schema is **live on Supabase** (project `iusnbmsmbgkevjjlpmck`):
7 tables, 9 views, applied as migrations `screener_schema_init` and
`screener_views_init`. `public` was not touched — 80 tables before and after,
and `public.market_snapshots` still holds its 89,574 rows.

Seeded with a small real sample (8 series, 66 open-market snapshots) purely to
prove the views compute on live data. **Those activity numbers are not the true
series totals** — the seed caps at 12 markets per series, so anything with a
wider ladder is undercounted. Real numbers arrive with the first Job A run.

Still to do, and all it needs is credentials on Railway:

| | needs |
|---|---|
| Job A / B on cron | `DATABASE_URL` (the Postgres connection string, not the MCP link) |
| Job C backfill | same, then run manually |
| Job D classify | `ANTHROPIC_API_KEY` |
| Daily prune | `sql/020_prune.sql` as a cron step |

## Storage

Measured, not estimated: bytes/row from a real Postgres 16 after ingesting live
rows; volumes from full page-walks of the live API.

| table | rows/day | bytes/row | per day |
|---|---:|---:|---:|
| `market_snapshots` | 78,354 open × 4 runs = 313,416 | 296 | **88.5 MB** |
| `markets_terminal` | 66,005 settled | 2,263 | **142.4 MB** |
| `market_taker_stats` | 17,078 (volume ≥ 1) | 240 | **3.9 MB** |
| `series` / `events` | upsert-in-place | 1,767 / 1,151 | ~0 (21 MB fixed) |
| | | | **≈ 235 MB/day** |

That is **6.9 GB/month, 84 GB/year** — it fills Supabase Pro's included 8 GB in
about 35 days. Two levers, both off by default:

**1. Prune snapshots (recommended, no information loss).** `v_series_activity`
only reads the last 24 hours. Everything older is history. Run daily:

```bash
psql "$DATABASE_URL" -v days=30 -f sql/020_prune.sql
```

Caps `market_snapshots` at ~2.6 GB steady state instead of growing forever
(7 days ≈ 620 MB).

**2. `TERMINAL_MIN_VOLUME` (default `1`, a real tradeoff).** 74% of settled markets never
trade — auto-generated crypto strike ladders. They carry no tape, contribute
nothing to `v_taker_bleed`, and cost ~105 MB/day, most of it the `raw` jsonb
(1,481 of the 2,263 bytes/row). Skipping them cuts `markets_terminal` from
142 MB/day to 37 MB/day. It is a deliberate deviation from G4 — you lose the
record that those markets existed — and is on by default because the target
database already holds 17 GB. Set `TERMINAL_MIN_VOLUME=0` to keep everything.

With both applied: **~41 MB/day ongoing growth** (1.2 GB/month), plus the capped
snapshot window. That is ~135 days on Pro's included 8 GB.

`select * from v_storage;` reports live per-table footprint and bytes/row;
`select * from v_ingest_health;` reports freshness and whether a run is stale.

## The metric

For every **taker** fill in a settled market, we know what the aggressor paid
and what the contract was worth at settlement:

- taker bought YES at p, settled YES → `+(100 − p)` cents/contract
- taker bought YES at p, settled NO → `−p`
- mirrored for NO

Weighted by size and summed over the tape. **Negative = takers bled = makers
earned.** That is the evidence that a passive seat in the series was profitable,
derived without ever placing an order.

Each trade contributes exactly one term, on the taker's side only — counting
both sides sums to zero. `tools/cp3_handcheck.py` verifies this against three
real tapes using a second, independently written implementation, and also
reconciles tape contracts against each market's reported volume.

## Checkpoints

| | Gate | Status |
|---|---|---|
| CP1 | Spec + fixtures vendored, diffed, DDL written | done — see `DISCREPANCIES.md` |
| CP2 | Job A on cron, two runs, spot-check 3 tickers | needs `DATABASE_URL` |
| CP3 | Tape math matches hand-computed PnL to the cent | **passing** — `tools/cp3_handcheck.py` |
| CP4 | Job D complete, ≥25-row human review under 10% error | needs credentials |
| CP5 | `v_screen` returns rows; temperature series classify correctly | needs CP2–CP4 |

`v_screen_funnel` shows how many series survive each filter, so an empty screen
tells you which stage emptied it. `v_cp5_ground_truth` is the built-in check
that the classifier works at all.

## Notes

Read `DISCREPANCIES.md` before changing anything that touches the API. The live
API differs substantially from the build spec's assumptions — prices and
volumes are decimal strings rather than integer cents, contracts are fractional,
settled markets report status `finalized`, and combo (multivariate) markets must
be filtered out or they dominate the results.
