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

## D28 — BREAKING: the "scrapable public source" filter is a no-op as specified.

Measured over all 12,564 live series: **12,557 (99.94%) publish a
`settlement_sources` entry with a non-empty URL.** Filtering on the presence of
a settlement source removes **7 series**. The spec leans on this filter as one
of its four load-bearing screens; as written it screens nothing.

The signal is in the *shape* of the URL, not its existence. Across the 2,230
recurring series, 2,357 settlement URLs point at a bare homepage and only 1,257
carry a real path. `KXIMOCOUNTRY` settles to `https://www.wsj.com/` — a
newsroom front page, not a data endpoint.

Domain is what discriminates:

| scrapable | count | media homepage | count |
|---|---:|---|---:|
| bls.gov | 204 | espn.com | 503 |
| forecast.weather.gov + weather.gov | 98 | foxsports.com | 245 |
| cfbenchmarks.com | 139 | wsj.com | 159 |
| fred.stlouisfed.org | 37 | apnews.com | 121 |

Consequence: `settlement_source_type` is derived from a domain allowlist
(`lib/classify_rules.py`), not from the presence of a source.

## D29 — `fee_type: quadratic_with_maker_fees` is a free disqualifier.

130 series charge makers as well as takers (107 of them Sports). The premise of
the screen is that a *passive* seat earns; paying maker fees inverts that. The
field is published per series, so this costs nothing to check, and `v_screen`
now excludes it. Upgrades U2 from "flagged, not modeled" to an actual filter.

## D30 — Two classifier bugs found by CP5, and CP5's own scope was wrong.

Running the deterministic rules against the real payloads surfaced three
distinct faults:

1. **Exact domain matching silently misroutes subdomains.** `KXHIGHUS` settles
   to `wpc.ncep.noaa.gov`, which does not equal `noaa.gov`, so it fell to
   `unknown`. Domain tests must be suffix matches.
2. **Category is not a reliable benchmark proxy.** `KXHIGHNY0` ("NYC high
   temperature") is filed under category `World`, not `Climate and Weather`, so
   a category-keyed rule gave it `benchmark = unknown`. The settlement domain
   has to win over the category label.
3. **`v_cp5_ground_truth` matched the wrong series.** `like 'KXLOW%'` also
   matches **Lowe's** — `KXLOW`, `KXLOWA` ("Lowe's Annual KPI"), `KXLOWCC`
   ("Lowe's Credit Card Spend") — and `like 'KXHIGH%'` matches `KXHIGHMOVDJT`
   ("Highest margin of victory"). Those are one_off Financials/Politics series
   that *correctly* fail the temperature check, so the gate read 50/61 and
   indicted a working classifier. Scoped to `category = 'Climate and Weather'`
   it reads **52/53** — the residual is `KXHIGHTEMPDEN`, frequency `custom`,
   which D9 leaves ambiguous by design.

## D31 — Summed volume cannot distinguish a live ladder from a dead one.

`v_series_activity` originally took the median spread across *every* open
market and summed volume across all of them. Both mislead on wide strike
ladders.

`KXUE` (Monthly Unemployment) ranked **first** on median spread at 60¢ — which
looked like a large maker opportunity. In fact only **7 of its 68 open markets
had any 24h volume at all**, and 94% of that volume sat in a single contract.
The 60¢ was the width of dead strikes nobody quotes. `KXCPICORE` was the same
shape (8 of 55 live). Meanwhile `KXHIGHTDC` had 12 of 12 live at a 1¢ spread.

Two fixes: spread is now measured only over markets with `volume_24h > 0`, and
`breadth` (live markets ÷ open markets) plus `top_market_share` are exposed and
filtered at `breadth >= 0.34`. Without this the screen ranks dead ladders top.

## D32 — Job C's series scope was effectively unbounded.

`recurring_series()` selected `frequency not in ('one_off','')`, which admits
`custom` — 5,387 of 12,564 series — so the walk covered **7,617 series** rather
than the 643 that actually recur and trade. It also admitted the
high-frequency crypto ladders: `KXSOLE` alone carries **350 open strikes**,
which at hourly × 180 days is on the order of a million markets for one series.

Scope is now three-tier: `BACKFILL_SERIES` (explicit), `BACKFILL_FROM_SCREEN=1`
(the surviving candidates), or recurring frequencies only. Scoped to the ~85
survivors the backfill is ~91k markets and ~64k tape requests — hours, not
weeks.

---

## D33 — `frequency: custom` was silently costing the screen real candidates.

D9 reads `recurrence` straight off `series.frequency` and refuses to guess, so
all 5,393 `custom` series land in `unknown`. `v_screen` requires
`recurrence = 'recurring'`, which means every one of them is excluded — 43% of
the live universe, dropped on the strength of a label the exchange declines to
fill in.

That is the right instinct and the wrong outcome. `custom` is not a cadence,
it is the absence of one, and a minority of those series are plainly periodic.
Measured against the first full Job A run: 1,163 `custom` series have live
activity, 228 sit in the volume band with a breathing ladder, and **5** clear
every screen filter except this one. Three of them survive the settlement
source test too — `KXCBDECISIONMEXICO` (vol 21,103, breadth 0.905),
`KXRAINMIAM` and `KXHMONTHRANGE`. Central bank decisions and monthly weather
ranges are exactly the profile the instrument is hunting for.

The fix does not guess. It reads a *second structural fact*: how many distinct
events the series has already produced. A series with a real history of events
recurs as a matter of record. `RECURRING_EVENT_FLOOR = 6` recovers all three
candidates while reclassifying 931 series; a floor of 4 recovers the same three
and touches 1,056. Reclassification alone changes nothing downstream — a series
still has to pass benchmark, settlement source, volume band and breadth — so
the floor cannot widen the screen on its own.

`recurrence()` stays a pure function and keeps its old behaviour for any caller
that does not supply `n_events`. Promoted rows are marked in `series_tags.notes`
(`[recurrence from N events]`) so a reviewer can tell a recovered tag from a
structural one.

`RULES_VERSION` is now `rules:v2`, and Job D re-tags any unreviewed row whose
stored `model` differs from the current version. Previously a rule change only
reached series tagged after it, leaving everything else stale until somebody
remembered `--recheck`.

---

## D34 — CRITICAL: the screen could only ever return weather and economics.

`benchmark()` enumerated Sports, Crypto, Financials, Politics, Elections,
Climate and Weather and Economics, then ended `return "unknown", None`.
`v_screen` keeps only `benchmark = 'none'`, and only three branches could
produce it — the weather domains, Climate and Weather, and non-rates
Economics. **Every category nobody thought to name fell through to `unknown`
and was silently dropped**, no matter how well it scored on every other filter.

This is not a tuning issue. It made the instrument structurally incapable of
answering the question it was built to ask. Of the 163 series that are
recurring, in the volume band and carrying a live ladder:

| category | verdict | n | vol24h | |
|---|---|---:|---:|---|
| Climate and Weather | `none` | 47 | 588k | kept |
| Sports | `sportsbook` | 46 | 821k | correctly cut |
| Entertainment | `unknown` | 17 | 257k | **cut by fall-through** |
| Crypto | `spot_crypto` | 13 | 217k | correctly cut |
| Economics | `none` | 12 | 180k | kept |
| Commodities | `unknown` | 9 | 94k | **cut by fall-through** |
| Politics | `other_liquid_market` | 8 | 50k | correctly cut |
| Financials | `cme_or_rates` | 6 | 96k | correctly cut |
| Science and Technology | `unknown` | 3 | 49k | **cut by fall-through** |

47 + 12 ≈ the 56 that survived. The screen was reporting the answer it was
built to be able to reach, which looked like a finding about Kalshi and was
actually a finding about the rules.

Adjudicating the 29 orphans rather than defaulting them:

- **Commodities** settle to `theice.com` and Pyth — ICE futures and an oracle
  price feed. Those *are* sharp benchmarks, so the exclusion was right and the
  reasoning was absent. `MARKET` now carries those domains and is checked
  early, since settling to a price feed makes that feed the benchmark whatever
  the category says. `Commodities` also joins `Financials`.
- **Entertainment and Science and Technology** mostly have no external book at
  all: YouTube daily views, Netflix Top 10 ranks, Luminate album units,
  Billboard chart position, Spotify monthly listeners, CDC measles counts,
  LMArena model rankings. These are scrapable numbers on a schedule with
  nobody quoting a sharp market against them — the exact profile in §1.
- **Awards** (Oscars, Emmys, TIME) are genuine committee decisions. They stay
  out via `committee_or_subjective`, which is the correct gate for them.

`billboard.com` and `luminatedata.com` were also sitting in `SUBJECTIVE` next
to the Oscars. A chart position is a published number, not a jury verdict, so
they move to `NUMERIC`; `time.com` moves in the other direction, since Person
of the Year is exactly a jury verdict.

The fall-through is gone. `benchmark()` enumerates the signals that say a sharp
book *exists* and answers `none` once all of them have been checked. `unknown`
is now reserved for a payload with neither a category nor a settlement
domain — the only case where the screen genuinely cannot reason.

Measured effect: **56 candidates → 70, of which 21 are non-weather**, carrying
~300k of daily volume that the screen previously could not see. CP5 ground
truth is unaffected (39 temperature series, 0 misclassified), and every
commodity and awards series verified still excluded. `RULES_VERSION` is
`rules:v3`.

---

## D35 — `BACKFILL_SERIES` did not bound the half of Job C that costs anything.

D32 scoped the *ingest*. `pending_markets()` was never scoped at all — it
selected from `markets_terminal` on settlement window and volume alone. That
would be harmless if `markets_terminal` only held the scoped series, but
`main()` runs two ingests:

```python
if BACKFILL_SOURCE != "recent":
    ingest_archive(conn, client, since)   # scoped to BACKFILL_SERIES
ingest(conn, client, since)               # global page-walk, unscoped
```

The second is described in-code as "one cheap global page-walk", and it is
cheap *to ingest*. What it is not is cheap downstream: it writes every settled
market on the exchange inside the window, and phase 2 then queues one tape
request per traded market across all of them.

Measured on the first real run, scoped to 70 series:

| | markets | series |
|---|---:|---|
| archive walk (scoped, correct) | 59,054 | 70 |
| `markets_terminal` after the global walk | 749,654 | 561 |
| phase 2 worklist as written | 239,526 | 561 |
| phase 2 worklist scoped | **52,707** | 70 |

At the client's 5 req/s ceiling — and it spends most of its time throttled to
2.5 by 429s — that is the difference between roughly 3–6 hours and 13–26, with
about 78% of the work spent on series that cannot reach the screen. 59 of the
70 in-scope series already carry the 20+ settled markets `v_screen` requires,
so the scoped worklist loses nothing the screen can use.

`pending_markets()` now takes the same scope the ingest used.

Two things left alone deliberately:

- The global recent walk still runs. It is genuinely cheap, and D26 means the
  archive can lag on very recently settled markets. It costs storage, not
  hours, and `TERMINAL_MIN_VOLUME` already governs that trade.
- `order by mt.volume asc` is §6's ordering and stays. Worth knowing when
  watching a run, though: the cheapest and least informative markets are taped
  first, so partial progress is not representative — a run killed at 50% has
  the *low* half of the volume distribution, not a random half.

---

## Unresolved / carried forward

- **U1 — RESOLVED by D26.** The `/historical/*` split is a retention boundary
  on the live endpoints, not a documented cutover. Measured at ~75 days on
  KXHIGHNY, but it is not a fixed global age — KXJOBLESS returns nothing live
  at any age, while high-volume series stay live longer. Treat the archive as
  the authority for anything older than a few weeks.
- **U2 — RESOLVED by D29.** `fee_type: quadratic_with_maker_fees` (130 series)
  charges makers too, so "maker PnL ≈ −taker PnL" (§7) overstates maker
  economics there. Rather than model it, `v_screen` now excludes those series
  outright — a passive seat paying maker fees is not the thing being screened for.
- **U3 — exact fee schedule PDF** not diffed. §7 explicitly permits the
  approximation for v1; `fee_multiplier` (D10) closes the worst error.
- **U4 — RESOLVED by D33.** `frequency: "custom"` covers 5,393 series and is
  ambiguous as a *label*, but not as a *record*: the number of events a series
  has already produced settles it without guessing. Series below the floor
  still land in `unknown`, so the ambiguity is narrowed rather than papered
  over.

---

## D36 — a dropped connection hangs the job instead of failing it.

Job C ran cleanly for three and a half hours, reached 36,200 of 54,522 markets,
and then stopped dead. No exception, no log line, no exit. CPU fell to 0.0002
and memory froze at 0.08402944 GB — identical across 61 consecutive samples —
and stayed that way for 90 minutes until the run was killed.

It was not the API client. `KalshiClient.get()` sets `timeout=45`, bounds
retries, and logs a warning on every retry path, so a stall there would have
left a trail. There was nothing after `aggregated 36200/54522`.

`aggregate_all()` commits *before* it logs:

```python
if done % 25 == 0:
    conn.commit()
    db.set_job_cursor(...)
    conn.commit()
    log.info("aggregated %d/%d ...")
```

so a hang inside the commit leaves exactly this signature — the previous
batch's line as the last thing written.

`psycopg.connect()` was called with no `connect_timeout` and no TCP keepalives.
When Supabase's pooler drops a connection the client blocks forever on a socket
read that will never return: no timeout, so no exception, so nothing to log and
nothing to exit with.

The failure shape is the problem as much as the failure. A crash would have
been fine — every job here exits 1 on error and a failed run is visible as
failed. A silent stall is invisible: `restartPolicyType: NEVER` means nothing
restarts it, Railway still reports the deployment `SUCCESS`, and the only
symptom is a row count that stops moving. This one was caught by a scheduled
check comparing log timestamps against wall clock, not by anything the system
reports on its own.

`connect()` now sets `connect_timeout=15` and keepalives (`idle=30`,
`interval=10`, `count=5`), so a dead peer surfaces as an `OperationalError`
within ~80 seconds and the job fails loudly like every other error path.

Job C is resumable — `pending_markets()` skips anything already in
`market_taker_stats` — so the recovery is a redeploy, and the cost of the stall
was wall-clock only.

---

## Hourly temperature markets (KXTEMP*H), phase 1 — 2026-08-28

**D37 — `market_trades.price_cents` is NUMERIC, not INT.**
The build spec says "Cents integers". 1,099 KXTEMPNYCH markets between
2026-03-24 and 2026-03-30 are `tapered_deci_cent` and quote in tenths of a
cent, so an integer column would silently round 1.6% of the tape — and those
are exactly the tickers CP4's 1% PnL reproduction would then fail on, with no
way to tell rounding from a real disagreement. Numeric is strictly wider: every
`linear_cent` market still stores whole cents. The same reasoning applies to
the cents columns on `market_candles_1m`.

**D38 — the live and historical candlestick endpoints use different field
names for the same data.** Verified on both, same market shape:

| | live `/series/{s}/markets/{t}/candlesticks` | historical `/historical/markets/{t}/candlesticks` |
|---|---|---|
| bid/ask OHLC | `open_dollars`, `close_dollars`, … | `open`, `close`, … |
| traded price | `price.close_dollars` | `price.close` |
| volume | `volume_fp` | `volume` |
| open interest | `open_interest_fp` | `open_interest` |

Reading the historical endpoint with the live names does **not** error: every
price parses to None and the loader writes a full set of NULL-priced rows that
look like a market nobody quoted. `lib/hourly.py:candle_row` accepts either
spelling, and `jobs/hourly_candles_backfill.py` logs the live/historical split
so a silent all-NULL load is visible in the run output.

**D39 — candlestick `start_ts`, `end_ts` and `period_interval` are all
required.** Omitting any of them is a 400, on both endpoints. The build spec's
sketch (`?period_interval=1`) would never have returned a row.

**D40 — endpoint choice is governed by `GET /historical/cutoff`, which moves.**
`market_settled_ts` read `2026-06-29T00:00:00Z` when this was written. Markets
settling before it are served ONLY by the `/historical/*` endpoints. The value
is fetched at job start rather than hardcoded.

**D41 — `markets_terminal` has a seven-week hole in KXTEMPNYCH.**
Nothing settles between 2026-06-06 02:00Z and 2026-07-25 12:00Z. Job B is
high-water-mark driven and only moves forward, so a window it was not running
for is never revisited. The hole swallows all three CP2 reference events. The
markets are still retrievable from the live `/markets` endpoint (they settled
after the cutoff), which accepts `min_close_ts`/`max_close_ts` and reached
2026-07-03 in testing — hence `jobs/hourly_markets_gap.py`.

**D42 — screener tables must NOT be registered in `public.retention_policy`.**
`public.downsample_table` interpolates the policy's `table_name` with
`format(... %I ...)`, which quotes the whole string as ONE identifier: a row
naming `screener.market_trades` becomes `"screener.market_trades"`, a table of
that literal name. Its `search_path` is also pinned to `pg_catalog, public`, so
an unqualified screener table cannot resolve either. `public.run_retention`
wraps each table in its own exception block, so such a row would not break
retention for the live weather tables — it would report an error every night
forever while pruning nothing. Screener retention stays in pg_cron, alongside
the existing `screener-prune-snapshots`.

**D43 — the candlestick API is already sparse, but not change-only.**
A 60-minute market returned 26 candles, not 60. It omits some minutes on its
own, yet still returns quiet minutes whose quotes are unchanged, so the
change-only filter in `lib/hourly.py:is_change_row` is applied on top rather
than assumed. The first row of each market is always kept: it is the T-60
opening book that CP5 reconciles against `market_snapshots`.
