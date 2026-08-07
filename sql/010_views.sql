-- Kalshi long-tail screener — screen views (build-spec §9)
set search_path to screener;
-- Band and thresholds are starting values, not conclusions — tune after CP5.

-- Dropped and recreated rather than CREATE OR REPLACE'd: replace cannot change
-- a column's data type, so a type correction in any view would otherwise fail
-- on an existing database and leave this file only conditionally re-runnable.
drop view if exists v_screen cascade;
drop view if exists v_screen_funnel cascade;
drop view if exists v_cp5_ground_truth cascade;
drop view if exists v_ingest_health cascade;
drop view if exists v_storage cascade;
drop view if exists v_bleed_exclusions cascade;
drop view if exists v_taker_bleed cascade;
drop view if exists v_series_activity cascade;
drop view if exists v_latest_snapshot cascade;

-- ---------------------------------------------------------------- activity
-- Latest snapshot per still-open market, from recent runs only.
--
-- Two deviations from §9, both load-bearing.
--
-- First: §9 aggregates *every* snapshot in a 30-day window. Job A runs 4x/day,
-- so that is ~120 rows per market, each holding an overlapping rolling-24h
-- volume figure. Summing them inflates sum_vol24h_latest by roughly 120x and
-- makes the `between 1000 and 100000` band meaningless. We take one row per
-- market.
--
-- Second: the window is 24h, not 30 days. `distinct on (ticker)` over 30 days
-- returns the last snapshot of every market seen that month — including markets
-- that have since closed — so they keep inflating open_markets and contributing
-- a stale volume_24h long after they stopped trading. Since the underlying
-- metric *is* a rolling 24h figure, 24h is also the only window over which
-- summing it is coherent. Closed/settled statuses are excluded outright.
--
-- Job A runs every 6h, so 24h tolerates three consecutive missed runs. If Job A
-- has been down longer than that this view empties out and v_screen returns
-- nothing — visible in v_screen_funnel and v_ingest_health, which is the
-- honest failure mode rather than silently screening on stale prices.
create view v_latest_snapshot as
select distinct on (ticker) *
from market_snapshots
where run_ts > now() - interval '24 hours'
  and status not in ('closed', 'settled', 'finalized')
order by ticker, run_ts desc;

create view v_series_activity as
select s.series_ticker,
       count(distinct ls.ticker)                            as open_markets,
       count(distinct ls.ticker) filter (where ls.volume_24h > 0)
                                                            as live_markets,
       -- Breadth: what fraction of the strike ladder actually trades. Summed
       -- volume alone cannot tell a healthy distributed series from a dead
       -- ladder with one hot strike, and the difference decides whether a
       -- passive seat is fillable at all. Measured: KXUE looked tradeable on
       -- volume (1,444/day) but only 7 of 68 markets had any, with 94% of it
       -- in a single contract; KXHIGHTDC had 12 of 12 live. See D31.
       round(count(distinct ls.ticker) filter (where ls.volume_24h > 0)::numeric
             / nullif(count(distinct ls.ticker), 0), 3)     as breadth,
       -- percentile_cont returns double precision; cast back so the whole
       -- pipeline stays in numeric and money never round-trips through a float.
       percentile_cont(0.5) within group (order by ls.volume_24h)::numeric
                                                            as med_mkt_vol24h,
       sum(ls.volume_24h)                                   as sum_vol24h_latest,
       max(ls.volume_24h) / nullif(sum(ls.volume_24h), 0)   as top_market_share,
       -- Spread over markets that TRADE. Including untraded strikes drags the
       -- median toward the width of dead contracts, which inverts the ranking:
       -- the widest "spreads" in the first live run were all dead ladders.
       percentile_cont(0.5) within group (
         order by (ls.yes_ask - ls.yes_bid)) filter (where ls.volume_24h > 0)
                                                       ::numeric as med_spread_dollars,
       (percentile_cont(0.5) within group (
         order by (ls.yes_ask - ls.yes_bid)) filter (where ls.volume_24h > 0)
        * 100)::numeric                                      as med_spread_cents,
       max(ls.run_ts)                                       as as_of
from series s
join v_latest_snapshot ls on ls.series_ticker = s.series_ticker
group by 1;

-- ---------------------------------------------------------------- bleed
-- Negative = takers bled = makers earned (§7).
-- taker_fee_est_cents is stored as a positive cost magnitude, so subtracting it
-- makes a bleeding series look worse, which is the intended direction.
create view v_taker_bleed as
select mt.series_ticker,
       count(*) filter (where not mts.truncated)                 as n_settled,
       count(*) filter (where mts.truncated)                     as n_truncated,
       sum(mts.contracts) filter (where not mts.truncated)       as contracts,
       sum(mts.taker_gross_pnl_cents) filter (where not mts.truncated)
         / nullif(sum(mts.contracts) filter (where not mts.truncated), 0)
         as gross_bleed_cents_per_ct,
       sum(mts.taker_gross_pnl_cents - coalesce(mts.taker_fee_est_cents, 0))
         filter (where not mts.truncated)
         / nullif(sum(mts.contracts) filter (where not mts.truncated), 0)
         as feeadj_bleed_cents_per_ct
from markets_terminal mt
join market_taker_stats mts using (ticker)
where mt.result in ('yes', 'no')   -- voids/scalar settles counted elsewhere, excluded here
group by 1;

-- Markets excluded from the bleed math, counted rather than silently dropped (§7).
create view v_bleed_exclusions as
select mt.series_ticker,
       count(*) filter (where mt.result not in ('yes', 'no')) as n_non_binary_result,
       count(*) filter (where mts.truncated)                  as n_truncated
from markets_terminal mt
left join market_taker_stats mts using (ticker)
group by 1;

-- ---------------------------------------------------------------- the screen
create view v_screen as
select s.series_ticker, s.title, s.category, s.frequency,
       t.settlement_source_type, t.benchmark, t.recurrence, t.scrape_difficulty,
       a.open_markets, a.live_markets, a.breadth, a.top_market_share,
       a.sum_vol24h_latest, a.med_spread_cents,
       b.n_settled, b.contracts,
       b.gross_bleed_cents_per_ct, b.feeadj_bleed_cents_per_ct,
       s.fee_type, s.fee_multiplier
from series s
join series_tags t using (series_ticker)
left join v_series_activity a using (series_ticker)
left join v_taker_bleed b using (series_ticker)
where t.recurrence = 'recurring'
  and t.benchmark = 'none'
  and t.settlement_source_type in ('scrapable_numeric_feed', 'official_report')
  -- Paying maker fees inverts the entire premise of a passive seat. Published
  -- per series, so this costs nothing to check (D29).
  and coalesce(s.fee_type, '') <> 'quadratic_with_maker_fees'
  and coalesce(a.sum_vol24h_latest, 0) between 1000 and 100000   -- contracts/day band, tune
  -- At least a third of the ladder must actually trade (D31).
  and coalesce(a.breadth, 0) >= 0.34
  and coalesce(b.n_settled, 0) >= 20
order by b.feeadj_bleed_cents_per_ct asc nulls last;  -- most-negative = takers bleed most

-- ---------------------------------------------------------------- diagnostics
-- v_screen is four ANDed filters; when it returns nothing this says which one
-- emptied it, instead of leaving you to bisect the WHERE clause by hand.
create view v_screen_funnel as
select
  (select count(*) from series)                                          as series_total,
  (select count(*) from series_tags)                                     as classified,
  (select count(*) from series_tags where recurrence = 'recurring')      as recurring,
  (select count(*) from series_tags where recurrence = 'recurring'
      and benchmark = 'none')                                            as no_benchmark,
  (select count(*) from series_tags where recurrence = 'recurring'
      and benchmark = 'none'
      and settlement_source_type in ('scrapable_numeric_feed', 'official_report'))
                                                                         as scrapable,
  (select count(*) from series_tags t join series s using (series_ticker)
    where t.recurrence = 'recurring' and t.benchmark = 'none'
      and t.settlement_source_type in ('scrapable_numeric_feed', 'official_report')
      and coalesce(s.fee_type, '') <> 'quadratic_with_maker_fees')       as no_maker_fees,
  (select count(*) from v_series_activity
     where sum_vol24h_latest between 1000 and 100000)                    as in_volume_band,
  (select count(*) from v_series_activity
     where sum_vol24h_latest between 1000 and 100000
       and breadth >= 0.34)                                              as ladder_alive,
  (select count(*) from v_taker_bleed where n_settled >= 20)             as enough_settled,
  (select count(*) from v_screen)                                        as passes_all;

-- CP5 ground truth: the daily temperature series must classify as
-- scrapable_numeric_feed + benchmark=none + recurring. If they don't, the
-- classifier is broken — fix it before trusting anything else in the screen.
--
-- The ticker prefix alone is NOT sufficient. `like 'KXLOW%'` also matches
-- Lowe's (KXLOW, KXLOWA "Lowe's Annual KPI", KXLOWCC "Lowe's Credit Card
-- Spend") and `like 'KXHIGH%'` matches KXHIGHMOVDJT ("Highest margin of
-- victory"). Those are one_off Financials/Politics series that correctly fail
-- the check, so the unqualified pattern reported 50/61 and made a working
-- classifier look broken. Constraining to the weather category gives 52/53 —
-- the residual is KXHIGHTEMPDEN, whose frequency is `custom` (D30).
create view v_cp5_ground_truth as
select s.series_ticker, s.title, s.frequency,
       t.settlement_source_type, t.benchmark, t.recurrence,
       (t.settlement_source_type = 'scrapable_numeric_feed'
        and t.benchmark = 'none'
        and t.recurrence = 'recurring')                as passes
from series s
left join series_tags t using (series_ticker)
where (s.series_ticker like 'KXHIGH%' or s.series_ticker like 'KXLOW%')
  and s.category = 'Climate and Weather';

-- ---------------------------------------------------------------- health
-- Freshness and volume, so a stale or half-run pipeline is visible rather than
-- quietly producing a screen built on old data.
create view v_ingest_health as
select
  (select max(run_ts) from market_snapshots)                     as last_snapshot_run,
  (select round(extract(epoch from now() - max(run_ts)) / 3600, 1)
     from market_snapshots)                                      as snapshot_age_hours,
  (select count(*) from v_latest_snapshot)                       as markets_in_latest_window,
  (select max(ingested_at) from markets_terminal)                as last_terminal_ingest,
  (select count(*) from markets_terminal
    where ingested_at > now() - interval '24 hours')             as terminal_last_24h,
  (select count(*) from market_taker_stats
    where computed_at > now() - interval '24 hours')             as tapes_last_24h,
  (select count(*) from markets_terminal where series_ticker is null)
                                                                 as unlinked_terminal;

-- ---------------------------------------------------------------- storage
-- Per-table footprint and bytes/row, for capacity planning against the
-- Supabase tier. market_snapshots is append-only and needs pruning (see
-- tools/prune.sql); everything else is upsert-in-place or bounded.
create view v_storage as
select c.relname as table_name,
       (select reltuples::bigint from pg_class x where x.oid = c.oid) as est_rows,
       pg_size_pretty(pg_total_relation_size(c.oid))                  as total,
       pg_size_pretty(pg_relation_size(c.oid))                        as heap,
       pg_size_pretty(pg_indexes_size(c.oid))                         as indexes,
       pg_size_pretty(coalesce(pg_total_relation_size(c.reltoastrelid), 0)) as toast_raw_jsonb,
       case when (select reltuples from pg_class x where x.oid = c.oid) > 0
            then round(pg_total_relation_size(c.oid)
                       / (select reltuples from pg_class x where x.oid = c.oid))
       end                                                            as bytes_per_row
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'screener' and c.relkind = 'r'
order by pg_total_relation_size(c.oid) desc;
