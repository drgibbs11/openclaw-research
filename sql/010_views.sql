-- Kalshi long-tail screener — screen views (build-spec §9)
-- Band and thresholds are starting values, not conclusions — tune after CP5.

-- ---------------------------------------------------------------- activity
-- One row per series, computed from the MOST RECENT snapshot of each market.
--
-- Deviation from §9, and the reason: §9 aggregates every snapshot in a 30-day
-- window. Job A runs 4x/day, so that is ~120 rows per market, each holding an
-- *overlapping rolling 24h* volume figure. Summing them inflates
-- `sum_vol24h_latest` by roughly 120x, which makes the `between 1000 and 100000`
-- band in v_screen meaningless. Restricting to each market's latest snapshot
-- makes the column mean what its name says.
create or replace view v_latest_snapshot as
select distinct on (ticker) *
from market_snapshots
where run_ts > now() - interval '30 days'
order by ticker, run_ts desc;

create or replace view v_series_activity as
select s.series_ticker,
       count(distinct ls.ticker)                            as open_markets,
       percentile_cont(0.5) within group (order by ls.volume_24h) as med_mkt_vol24h,
       sum(ls.volume_24h)                                   as sum_vol24h_latest,
       percentile_cont(0.5) within group (order by (ls.yes_ask - ls.yes_bid))
                                                            as med_spread_dollars,
       percentile_cont(0.5) within group (order by (ls.yes_ask - ls.yes_bid)) * 100
                                                            as med_spread_cents,
       max(ls.run_ts)                                       as as_of
from series s
join v_latest_snapshot ls on ls.series_ticker = s.series_ticker
group by 1;

-- ---------------------------------------------------------------- bleed
-- Negative = takers bled = makers earned (§7).
-- taker_fee_est_cents is stored as a positive cost magnitude, so subtracting it
-- makes a bleeding series look worse, which is the intended direction.
create or replace view v_taker_bleed as
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
create or replace view v_bleed_exclusions as
select mt.series_ticker,
       count(*) filter (where mt.result not in ('yes', 'no')) as n_non_binary_result,
       count(*) filter (where mts.truncated)                  as n_truncated
from markets_terminal mt
left join market_taker_stats mts using (ticker)
group by 1;

-- ---------------------------------------------------------------- the screen
create or replace view v_screen as
select s.series_ticker, s.title, s.category, s.frequency,
       t.settlement_source_type, t.benchmark, t.recurrence, t.scrape_difficulty,
       a.open_markets, a.sum_vol24h_latest, a.med_spread_cents,
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
  and coalesce(a.sum_vol24h_latest, 0) between 1000 and 100000   -- contracts/day band, tune
  and coalesce(b.n_settled, 0) >= 20
order by b.feeadj_bleed_cents_per_ct asc nulls last;  -- most-negative = takers bleed most

-- ---------------------------------------------------------------- diagnostics
-- v_screen is four ANDed filters; when it returns nothing this says which one
-- emptied it, instead of leaving you to bisect the WHERE clause by hand.
create or replace view v_screen_funnel as
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
  (select count(*) from v_series_activity
     where sum_vol24h_latest between 1000 and 100000)                    as in_volume_band,
  (select count(*) from v_taker_bleed where n_settled >= 20)             as enough_settled,
  (select count(*) from v_screen)                                        as passes_all;

-- CP5 ground truth: the daily temperature series must classify as
-- scrapable_numeric_feed + benchmark=none + recurring. If they don't, the
-- classifier is broken — fix it before trusting anything else in the screen.
create or replace view v_cp5_ground_truth as
select s.series_ticker, s.title, s.frequency,
       t.settlement_source_type, t.benchmark, t.recurrence,
       (t.settlement_source_type = 'scrapable_numeric_feed'
        and t.benchmark = 'none'
        and t.recurrence = 'recurring')                as passes
from series s
left join series_tags t using (series_ticker)
where s.series_ticker like 'KXHIGH%' or s.series_ticker like 'KXLOW%';
