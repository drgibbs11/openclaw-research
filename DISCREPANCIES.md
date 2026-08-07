# DISCREPANCIES — spec v1 vs. live API

Verified against `spec/openapi.yaml` (downloaded from https://docs.kalshi.com/openapi.yaml)
and the fixtures in `fixtures/`, captured 2026-08-06.

Per G1, every `VERIFY` item in the build spec is resolved below. Items marked
**BREAKING** invalidate code or schema written to the spec's assumptions.

---

## D1 — BREAKING: there are no integer-cent price fields. Everything is a decimal string.

The build spec (§4, §11) assumes `yes_bid`, `yes_ask`, `last_price`, `volume`,
`volume_24h`, `open_interest`, `liquidity` as integers, prices "integer cents 1–99".

None of those fields exist on the `Market` schema. The live shape is:

| Spec assumed | Actual field | Type | Example |
|---|---|---|---|
| `yes_bid` (int cents) | `yes_bid_dollars` | `FixedPointDollars` (string) | `"0.0100"` |
| `yes_ask` | `yes_ask_dollars` | string | `"1.0000"` |
| `last_price` | `last_price_dollars` | string | `"0.0010"` |
| `volume` | `volume_fp` | `FixedPointCount` (string) | `"690.90"` |
| `volume_24h` | `volume_24h_fp` | string | `"0.00"` |
| `open_interest` | `open_interest_fp` | string | `"690.90"` |
| `liquidity` | `liquidity_dollars` | string | `"0.0000"` |

Same on `Trade`: `count` → `count_fp`, `yes_price` → `yes_price_dollars`,
`no_price` → `no_price_dollars`.

**Consequences:**
- All price math moves from integer cents to `numeric`. Storing cents as `int`
  would silently truncate. Schema uses `numeric` for prices and sizes.
- We keep the spec's *reporting* unit (cents per contract) so §7's numbers stay
  comparable, but compute in dollars and convert at the edge.

## D2 — BREAKING: contract counts are fractional.

`volume_fp: "595.30"`, `count_fp: "30.00"`, `open_interest_fp: "690.90"`.
Kalshi supports fractional contract sizes. The spec's `bigint` columns for
`volume`, `contracts`, `open_interest` are wrong — they'd truncate or error.
All count columns are `numeric` in the DDL.

## D3 — BREAKING: prices are not always whole cents.

Markets carry `price_level_structure` and a `price_ranges` array giving
`start`/`end`/`step`. Observed `step: "0.0010"` — tenths of a cent. So "integer
cents 1-99" (§11) is false for deci-cent markets. Both fields are persisted so
the tick regime is recoverable per market.

Measured values over ~5,000 open markets: `linear_cent` (2,992),
`deci_cent` (2,000), `tapered_deci_cent` (8). Note `linear_cent`, not `cent` —
an earlier draft of this entry guessed the plain form from the deci-cent
example and was wrong. Do not branch on an assumed two-value enum; the column
is stored verbatim and should be read, not predicted.

## D4 — status: the filter enum and the response enum are different vocabularies.

- Filter values accepted by `GET /markets?status=` (from spec):
  `unopened, open, paused, closed, settled`
- Value actually returned in `market.status` for a settled market: **`finalized`**

Querying `status=settled` returns objects whose `status` reads `finalized`.
The spec doc's guess (`unopened/open/closed/settled`) was right for the *filter*
and wrong for the *payload*. §11's "only ingest truly settled markets" is
implemented as: filter on `status=settled`, and treat payload status
`settled|finalized` as terminal. Anything else is not ingested into
`markets_terminal`.

## D5 — RESOLVED, and better than assumed: `GET /series` exists.

The spec said "`VERIFY` whether a list-all-series endpoint exists; if not,
derive the series set from events/markets." It exists.

- `GET /series` returns **all 12,525 series in a single unpaginated response**
  (~15.7 MB). No `limit`/`cursor` params.
- Supports `category`, `tags`, `include_product_metadata`, `include_volume`,
  and `min_updated_ts` for cheap incremental polling.
- The series object's primary key field is **`ticker`**, not `series_ticker`.

## D6 — RESOLVED: `min_ts`/`max_ts` are Unix **seconds** (int64), not RFC3339.

This was flagged in §11 as a silent-failure risk. Confirmed integer/int64 in the
spec for both `/markets/trades` and `/historical/trades`.

## D7 — RESOLVED, and it kills the Job B cost risk: settlement-time filters are server-side.

`GET /markets` accepts `min_settled_ts` / `max_settled_ts` (and
`min_close_ts`/`max_close_ts`, `min_created_ts`/`max_created_ts`).
The market object carries `settlement_ts`.

Job B's high-water mark is therefore a server-side filter, not a client-side scan
of every settled market. `settlement_ts` is the cursor field.

## D8 — NEW, and it matters: multivariate (combo) markets flood the default listing.

A large share of markets are auto-generated parlays — tickers like
`KXMVECROSSCATEGORY-*`, `KXMVESPORTSMULTIGAMEEXTENDED-*`, carrying
`mve_collection_ticker` and `mve_selected_legs`. All 3 markets in the first
naive `status=settled` pull were MVE. They are not recurring series settling to
a scrapable source and would badly pollute the screen.

`GET /markets` and `GET /historical/markets` both accept `mve_filter=exclude`.
**All ingest jobs pass `mve_filter=exclude`.**

## D9 — NEW: `recurrence` should not be an LLM field. The API gives it structurally.

`series.frequency` is a populated enum, not free text:

| frequency | count |
|---|---|
| custom | 5,387 |
| one_off | 4,908 |
| annual | 1,354 |
| monthly | 325 |
| weekly | 272 |
| daily | 203 |
| hourly | 55 |
| fifteen_min | 19 |

Asking the classifier to infer `recurrence` (§8) is strictly worse than reading
this field. `series_tags.recurrence` is now **derived**: `one_off` → `one_off`;
`fifteen_min|hourly|daily|weekly|monthly|annual` → `recurring`; `custom` →
`unknown` (the only case the classifier is asked to judge).

## D10 — NEW: real fee parameters are published per series.

`series.fee_type` ∈ {`quadratic` (12,395), `quadratic_with_maker_fees` (130)}
and `series.fee_multiplier` ∈ {0, 1}.

The spec's flat `7·p̂·(1−p̂)` approximation (§7) is the right *shape* for
`quadratic`, but **`fee_multiplier = 0` means no fee** and those series were
being charged a phantom ~1.75¢/contract at p=0.5. We now multiply the estimate
by the series' actual `fee_multiplier`. Still an approximation per §7 (per-order
round-up and maker/taker asymmetry are not modeled), and still not a fee engine
per §12 — but no longer wrong by construction on zero-fee series.

## D11 — NEW: block trades are on the public tape and must be excluded.

`Trade.is_block_trade` exists, and both trades endpoints accept
`is_block_trade=false`. Block trades are negotiated off-book at sizes and prices
that don't reflect a maker quoting the screen. Including them corrupts the
bleed metric. **All tape reads pass `is_block_trade=false`.**

## D12 — NEW: `taker_side` has two siblings; we use `taker_side`.

`Trade` carries `taker_side`, `taker_outcome_side`, and `taker_book_side`
(`ask`/`bid`). In the captured fixture `taker_side == taker_outcome_side == "no"`
with `taker_book_side == "ask"`. `taker_side` is the field named in §7 and is
the outcome the aggressor bought. `taker_book_side` is book geometry, not
outcome, and must not be used for PnL. All three are kept in `raw`.

## D13 — NEW: `/historical/trades` and `/historical/markets` exist.

Not mentioned in the build spec. Same param shape as the live endpoints. The
cutover age between `/markets/trades` and `/historical/trades` is not documented
in the spec file. Job C reads `/markets/trades` first and falls back to
`/historical/trades` when a 180-day-old market returns an empty tape.
**Open item — see "Unresolved" below.**

## D14 — Events carry `series_ticker` directly.

§11 warned "some markets' series linkage goes through the event object" — true,
and the event object does carry `series_ticker`, plus its own `category` and
`settlement_sources`. Market → event → series resolution works. Markets have no
`series_ticker` field at all, confirming the warning.

## D15 — Base URL.

`https://api.elections.kalshi.com/trade-api/v2` works unauthenticated (200) and
is listed in the spec's `servers:` block as "Production shared API server, also
supported". The spec's *first* listed server is
`https://external-api.kalshi.com/trade-api/v2`. Both are production. We default
to the elections host per §2/§3; `KALSHI_BASE_URL` overrides.
Note: `api.kalshi.com` (no subdomain prefix) is not a valid API host.

## D16 — G2 holds: no auth needed.

All five fixture captures returned 200 with no credentials. No 401s encountered.
No signing implemented, per G2.

## D17 — Rate limits are token-bucket, not fixed-window.

Per docs: independent Read and Write budgets, refilling continuously at a
per-second rate, bucket capacity 1–2 seconds of budget depending on tier. Basic
tier Read holds two seconds of budget, so short bursts are tolerated.
G6's default of 5 req/s with `Retry-After`-honoring backoff is well inside this;
kept unchanged.

## D18 — Series title/category live on the series; `series.title` is populated.

`KXHIGHNY` → `title: "Highest temperature in NYC"`, `category:
"Climate and Weather"`, `frequency: "daily"`, `tags: ["Daily temperature"]`,
`settlement_sources: [{name: "NWS Climatological Report", url: "..."}]`.
This is the CP5 ground-truth row and it has everything the classifier needs.

## D19 — Schema gap in the build spec: `job_state` was referenced but never defined.

§6 Job B requires a one-row `job_state` table for the high-water mark; §5's DDL
omits it. Defined in `sql/001_schema.sql` as a keyed table (not one-row) so Jobs
B and C can track cursors independently.

## D20 — `market_snapshots` has no `raw` column, against G4.

Deliberate deviation, called out rather than silently taken. At 4 runs/day over
several thousand open markets, a `raw jsonb` per snapshot row dominates storage
for no analytic gain — snapshots are a time series of ~10 numeric fields, and
G4's purpose (never lose data to schema drift) is served for the *entities*
(`series`, `events`, `markets_terminal`) which do keep `raw`. Snapshots instead
persist `price_level_structure` and the full quote set so the tick regime and
book state are recoverable.

## D21 — NEW: `/events` rejects `limit > 200`, contradicting its own spec.

`spec/openapi.yaml` documents `limit` on `GET /events` identically to every
other paginated endpoint: *"Number of results per page. Defaults to 100.
Maximum value is 1000."* The live endpoint returns
`400 {"error":{"code":"bad_request"}}` for any limit above 200.

Measured: 200 → 200 OK, 201 → 400, 300 → 400, 500 → 400, 1000 → 400.
`GET /markets` and `GET /markets/trades` do accept 1000, so this is specific to
`/events`, not a global cap. The client uses `EVENTS_MAX_LIMIT = 200`.

This one is worth noting as a process point: it is not visible in the vendored
spec, only from calling the endpoint. §2's verify-first protocol caught it
exactly where it was supposed to.

## D22 — Boolean query params.

Python's `str(False)` is `"False"`; the API happens to accept that spelling on
`with_nested_markets`, but the client normalizes booleans to `true`/`false`
before sending rather than relying on it.

## D23 — BREAKING FOR PLANNING: settled-market volume is ~70k/day, and 76% have no tape.

Measured by running Job B's sweep over a 2-day window:

| | count | share |
|---|---:|---:|
| terminal markets settled in 2 days | 140,338 | |
| …with zero volume (empty tape) | 106,052 | 76% |
| …with volume ≥ 1 | 34,255 | 24% |
| …with volume ≥ 100 | 26,243 | 19% |
| distinct events behind them | 7,314 | |

**Consequences the build spec did not anticipate:**

- §6's 180-day backfill covers on the order of **12M settled markets**, not the
  thousands the page-cap discussion implies. Ingest is fine (1000/page ≈ 12.6k
  requests), but a tape request per market is not: even restricted to the 24%
  with volume, that is ~3M requests ≈ 7 days of continuous paging at 5 req/s.
  **Plan Job C as a multi-day run, or narrow it** — shorten `BACKFILL_DAYS`,
  raise `BACKFILL_MIN_VOLUME`, or set `BACKFILL_EXCLUDE_CATEGORIES`.
- §6 applies a volume floor to Job C only. Job B had none, so it spent one
  request per zero-volume market — 76% of its work returning nothing. Both jobs
  now share `TAPE_MIN_VOLUME` (default 1).

The bulk of the zero-volume mass is auto-generated strike ladders on
high-frequency crypto series (`KXBTCD-*`, `KXHYPED-*`), which list a market per
strike per period and mostly never trade.

## D24 — `/events` is rate-limited harder than the other endpoints.

At 8 req/s a sustained `/events` page walk produced **179 consecutive 429s**;
`/markets` and `/series` sustained the same rate without complaint. Bursts of 8
against `/events` succeed, so this is bucket drain under sustained load rather
than a fixed per-endpoint cap.

This exposed a real defect in the client, now fixed: per-request exponential
backoff retried, succeeded, and then immediately resumed the same rate, draining
the bucket again. The client now widens its *sustained* interval on any 429
(halving the rate, floor 0.5 req/s) and recovers gradually after 20 clean
requests. At the spec's default 5 req/s the same walk settles at ~1.4–2.8 req/s
with a handful of 429s instead of a storm. G6 said "when in doubt, slower";
per-request backoff alone did not implement that.

## D25 — Markets can outrun their events, orphaning them from the screen.

Markets carry no `series_ticker` (D14), so linkage is market → event → series.
A market that settles before its event is synced lands with a NULL
`series_ticker`, and because `v_taker_bleed` groups by `series_ticker`, it is
invisible to the screen permanently — silently, with no error.

Two mitigations, both in `lib/tape.py` and called by Jobs B and C:
`resolve_missing_events()` fetches the specific events needed (7,314 for a
2-day window, not the full event list), and `repair_series_links()` relinks
affected rows afterwards.

**Rejected shortcut, recorded so it is not retried:** `event_ticker` looks like
`<series>-<date><suffix>`, so `split('-')[0]` appears to give the series ticker
for free. Measured over 600 events it agrees 582 times and **disagrees 18** —
legacy tickers predate the `KX` prefix (`JOBLESS` → `KXJOBLESS`, `AITURING` →
`KXAITURING`, `EUCLIMATE` → `KXEUCLIMATE`). A 3% silent misattribution rate on
the key the entire screen groups by is not acceptable, so linkage always goes
through the API.

## D26 — CRITICAL: `/markets` is a rolling recent window, not the full record.

Investigated after a question about whether public endpoints expose only a
subset of markets. **They do — but the cause is retention, not authentication.**

`GET /markets` only reaches back a limited window, and asking for more is
silently ignored rather than refused:

| `min_settled_ts` window | KXHIGHNY markets returned | oldest settlement |
|---|---:|---|
| 30 days | 180 | 2026-07-08 |
| 90 days | 444 | 2026-05-25 |
| 180 days | 444 | 2026-05-25 |
| 365 days | 444 | 2026-05-25 |
| 1000 days | 444 | 2026-05-25 |

`/historical/markets` for the same series returns markets back to **2021-08-08**.
Per series, live vs. archive:

| series | `/markets` | oldest | `/historical/markets` | oldest |
|---|---:|---|---:|---|
| KXHIGHNY | 444 | 2026-05-25 | 8,956 | 2021-08-08 |
| KXCPIYOY | 48 | 2026-06-10 | 555 | 2022-12-13 |
| KXFED | 22 | 2026-06-17 | 379 | 2021-09-24 |
| **KXJOBLESS** | **0** | — | **71** | 2021-08-14 |

The tape splits the same way: for an archived market, `/markets/trades` returns
**zero trades with no error**, while `/historical/trades` returns the real tape
(47, 44, and 1 trades on three sampled KXJOBLESS markets, which price correctly
through the §7 math).

**Why this was nearly fatal to v1.** Job C's 180-day backfill read `/markets`,
so it would have silently collected ~75 days and *nothing at all* for slower
series. KXJOBLESS and KXCPIYOY — weekly and monthly economic series settling to
published government statistics, with no sportsbook equivalent — are precisely
the profile `v_screen` exists to surface, and every one of them was invisible.
After the fix, KXCPIYOY produces 39 settled markets and −2.24 c/ct gross
(−3.21 fee-adjusted), clearing the `n_settled >= 20` threshold.

**Fix:** Job C now walks `/historical/markets` per recurring series
(`iter_historical_markets`). That endpoint has no time or status filter —
`min_settled_ts` is accepted and silently ignored — but returns newest-first,
so the walk stops once a page falls entirely outside the window. The cheap
global `/markets` walk still runs afterwards for anything listed since the
archive was last written. `lib/tape.py` already fell back to
`/historical/trades` on an empty tape, so the trade side needed no change.

### This is not an authentication boundary

Worth stating explicitly, since the natural reading is "we need an API key":

- The vendored spec declares `security` **only** on `/portfolio/*` order
  endpoints. Market-data paths inherit no global security requirement.
- Every market-data GET returns 200 unauthenticated. No 401 anywhere (G2).
- Within the live window the data is *complete*, not sampled. Per-status counts
  fetched independently sum exactly to the unfiltered walk (12 open + 444
  settled = 456). Three independent access paths — `/markets?series_ticker`,
  `/markets?event_ticker`, and `/events/{ticker}?with_nested_markets=true` —
  agree exactly on the same 6 markets for a sampled event.
- The apparently-truncated weather ladder is the real product shape, not a cap:
  the six strikes are `≤88°, 89–90, 91–92, 93–94, 95–96, ≥97`. The open-ended
  buckets at both ends partition every possible temperature, so no seventh
  market can exist without overlapping one of them. All 380 daily-high events
  sampled across five cities show exactly six.
- The "missing" history is fully retrievable **unauthenticated** from
  `/historical/*`.

Limitation, stated plainly: without a key we cannot prove an authenticated
`/markets` call returns no more than an unauthenticated one. But the missing
data is entirely recoverable without auth, so a key is not needed for v1, and
G2 forbids implementing signing regardless.

## D27 — `mve_filter` is rejected by `/historical/markets`.

The spec lists `mve_filter` on `/historical/markets` with enum `['exclude']`.
Sending that documented value returns `400 bad_request`, for every series
tested and at any limit. Combo markets are filtered client-side on that endpoint
instead (`mve_collection_ticker` is set on them). `/markets` accepts
`mve_filter` normally.

---

## Unresolved / carried forward

- **U1 — RESOLVED by D26.** The `/historical/*` split is a retention boundary
  on the live endpoints, not a documented cutover. Measured at ~75 days on
  KXHIGHNY, but it is not a fixed global age — KXJOBLESS returns nothing live
  at any age, while high-volume series stay live longer. Treat the archive as
  the authority for anything older than a few weeks.
- **U2 — `fee_type: quadratic_with_maker_fees`** (130 series) charges makers too.
  Our metric reports *taker* bleed, so maker fees don't enter the taker PnL — but
  "maker PnL ≈ −taker PnL" (§7) overstates maker economics on these 130 series.
  Flagged, not modeled, per §12.
- **U3 — exact fee schedule PDF** not diffed. §7 explicitly permits the
  approximation for v1; `fee_multiplier` (D10) closes the worst error.
- **U4 — `frequency: "custom"`** covers 5,387 series and is genuinely ambiguous;
  these are the only series where the classifier decides `recurrence`.
