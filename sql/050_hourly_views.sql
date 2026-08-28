-- Hourly temperature markets — phase 1 views.
--
-- Cross-schema joins into public are deliberate: the observation feeds live
-- there and copying them would create a second truth.

set search_path to screener;

-- ------------------------------------------------------- v_hourly_settle
-- One row per (series, close_time): what the ladder says the temperature was.
--
-- strike_f = ceil(T-value), because `-T76.99` is the "77 or above" rung.
-- settled_f = the highest strike that settled YES.
--
-- CENSORING IS NOT COSMETIC. If every rung settled yes the value was at or
-- above the top strike and settled_f is a LOWER BOUND; if every rung settled
-- no it was below the bottom strike and there is no yes rung at all, so
-- settled_f is NULL and the bound is min_strike. Treating either as a
-- measurement biases the basis check toward the middle of the ladder.
create or replace view v_hourly_settle as
with parsed as (
  select mt.series_ticker,
         mt.close_time,
         split_part(mt.ticker, '-', 2) as event_code,
         ceil((split_part(mt.ticker, '-T', 2))::numeric) as strike_f,
         mt.result
    from markets_terminal mt
   where mt.series_ticker like 'KXTEMP%H'
     and mt.ticker like '%-T%'
     and mt.result in ('yes', 'no')
     and mt.close_time is not null
)
select series_ticker,
       close_time,
       min(event_code)                                   as event_code,
       max(strike_f) filter (where result = 'yes')       as settled_f,
       count(*)                                          as n_strikes,
       count(*) filter (where result = 'yes')            as n_yes,
       min(strike_f)                                     as min_strike,
       max(strike_f)                                     as max_strike,
       count(*) filter (where result = 'no') = 0         as censored_high,
       count(*) filter (where result = 'yes') = 0        as censored_low,
       count(*) filter (where result = 'no') = 0
         or count(*) filter (where result = 'yes') = 0   as censored,
       -- Monotone means every yes strike sits below every no strike. CP6.
       coalesce(max(strike_f) filter (where result = 'yes'), '-Infinity')
         < coalesce(min(strike_f) filter (where result = 'no'), 'Infinity')
                                                         as monotone
  from parsed
 group by series_ticker, close_time;

comment on view v_hourly_settle is
  'Ladder-implied temperature per hourly event. settled_f is a BOUND, not a '
  'value, wherever censored is true.';


-- -------------------------------------------------- v_hourly_settle_vs_obs
-- The basis check: does the ladder agree with our own routine observation?
--
-- The comparison ob is the routine hourly METAR (product ASOS-HR, the :51)
-- reported in the hour ending at close_time. Not the 5-minute feed (ASOS-HFM)
-- and not SPECIs: the market settles against a routine hourly report, so
-- anything else is a different measurement being asked to agree.
--
-- KORD has no rows in public.observations and comes from wethr_nearby_obs
-- instead; that feed has a known outage 2026-08-05..2026-08-14.
create or replace view v_hourly_settle_vs_obs as
with mapped as (
  select s.*,
         case s.series_ticker
           when 'KXTEMPNYCH' then 'KNYC'
           when 'KXTEMPAUSH' then 'KAUS'
           when 'KXTEMPCHIH' then 'KORD'
           when 'KXTEMPDCH'  then 'KDCA'
           when 'KXTEMPLAXH' then 'KLAX'
           when 'KXTEMPMIAH' then 'KMIA'
           when 'KXTEMPBOSH' then 'KBOS'
         end as station
    from v_hourly_settle s
),
obs as (
  select m.series_ticker, m.close_time, m.station, m.settled_f, m.censored,
         (select o.temperature_f
            from public.observations o
           where o.station_code = m.station
             and o.product = 'ASOS-HR'
             and o.temperature_f is not null
             and o.observation_time >  m.close_time - interval '1 hour'
             and o.observation_time <= m.close_time
           order by o.observation_time desc
           limit 1) as obs_exact
    from mapped m
   where m.station is not null and m.station <> 'KORD'
  union all
  select m.series_ticker, m.close_time, m.station, m.settled_f, m.censored,
         (select w.temperature_f
            from public.wethr_nearby_obs w
           where w.neighbor_station = m.station
             and w.temperature_f is not null
             and w.obs_time_utc >  m.close_time - interval '1 hour'
             and w.obs_time_utc <= m.close_time
           order by w.obs_time_utc desc
           limit 1) as obs_exact
    from mapped m
   where m.station = 'KORD'
)
select series_ticker,
       station,
       close_time,
       settled_f,
       obs_exact,
       round(obs_exact)                       as obs_round,
       settled_f - round(obs_exact)           as diff_f,
       settled_f = round(obs_exact)           as match,
       abs(settled_f - round(obs_exact)) <= 1 as match_within_1,
       censored
  from obs;

comment on view v_hourly_settle_vs_obs is
  'Ladder settlement vs our routine hourly METAR. Censored rows compare a '
  'bound against a value and should be excluded before scoring the basis.';


-- ------------------------------------------------ v_hourly_bleed_by_minute
-- Where in the hour the resting side bleeds.
--
-- One pnl term per fill, on the taker's side only — counting both sides sums
-- to zero. Fee is the quadratic ceil(7*p*(1-p)) cents per contract, charged
-- once, matching lib/bleed.py so this view and market_taker_stats stay
-- comparable.
--
-- strike_dist = strike_f - settled_f: 0 is the rung that settled at the money,
-- negative is below it (settled yes), positive above (settled no).
create or replace view v_hourly_bleed_by_minute as
with t as (
  select tr.market_ticker,
         tr.series_ticker,
         tr.trade_time,
         tr.price_cents,
         tr."count"    as contracts,
         tr.taker_side,
         mt.close_time,
         mt.result,
         ceil((split_part(tr.market_ticker, '-T', 2))::numeric) as strike_f
    from market_trades tr
    join markets_terminal mt on mt.ticker = tr.market_ticker
   where mt.result in ('yes', 'no')
     and mt.close_time is not null
),
scored as (
  select t.*,
         s.settled_f,
         s.censored,
         extract(epoch from (t.close_time - t.trade_time)) / 60.0 as minutes_to_close,
         -- price_cents is the YES price; the NO taker paid the complement.
         case when t.taker_side = 'yes' then t.price_cents
              else 100 - t.price_cents end                        as paid_cents,
         case when t.taker_side = t.result then 100.0 else 0.0 end as payoff_cents
    from t
    left join v_hourly_settle s
           on s.series_ticker = t.series_ticker
          and s.close_time = t.close_time
)
select series_ticker,
       case when minutes_to_close <= 1  then '00-01'
            when minutes_to_close <= 5  then '01-05'
            when minutes_to_close <= 10 then '05-10'
            when minutes_to_close <= 15 then '10-15'
            when minutes_to_close <= 30 then '15-30'
            else '30-60' end                        as minutes_bucket,
       strike_f - settled_f                          as strike_dist,
       width_bucket(paid_cents, 0, 100, 10) * 10     as price_band,
       extract(hour from close_time)::int            as close_hour_utc,
       count(*)                                      as trades,
       sum(contracts)                                as contracts,
       -- Taker PnL per contract, gross and net of the entry fee.
       round(sum((payoff_cents - paid_cents) * contracts)
             / nullif(sum(contracts), 0), 3)         as taker_pnl_cents_per_contract,
       round(sum((payoff_cents - paid_cents) * contracts
                 - ceil(7.0 * (paid_cents/100.0) * (1 - paid_cents/100.0)) * contracts)
             / nullif(sum(contracts), 0), 3)         as taker_net_cents_per_contract,
       avg(paid_cents)                               as avg_paid_cents,
       bool_or(censored)                             as any_censored
  from scored
 group by 1, 2, 3, 4, 5;

comment on view v_hourly_bleed_by_minute is
  'Taker bleed by minutes-to-close x strike distance x price band. Negative '
  'taker_net = the aggressor lost, i.e. the resting side earned.';


-- ---------------------------------------------------- v_hourly_ask_ladder
-- The touch at fixed checkpoints through the hour, per market.
--
-- "Last stored row at or before that minute" — with change-only candles a
-- checkpoint minute usually has no row of its own, so carrying the last one
-- forward is the definition, not an approximation.
create or replace view v_hourly_ask_ladder as
with cps as (
  select * from (values (60), (45), (30), (15), (10), (5), (1)) as v(minutes_before)
),
mk as (
  select mt.ticker, mt.series_ticker, mt.close_time, mt.result,
         ceil((split_part(mt.ticker, '-T', 2))::numeric) as strike_f
    from markets_terminal mt
   where mt.series_ticker like 'KXTEMP%H'
     and mt.ticker like '%-T%'
     and mt.result in ('yes', 'no')
     and mt.close_time is not null
)
select mk.ticker,
       mk.series_ticker,
       mk.close_time,
       mk.result,
       mk.strike_f,
       s.settled_f,
       mk.strike_f - s.settled_f as strike_dist,
       cps.minutes_before,
       c.bucket_time,
       c.yes_bid_close_cents,
       c.yes_ask_close_cents,
       c.yes_ask_close_cents - c.yes_bid_close_cents as spread_cents
  from mk
  cross join cps
  left join v_hourly_settle s
         on s.series_ticker = mk.series_ticker and s.close_time = mk.close_time
  left join lateral (
        select cd.bucket_time, cd.yes_bid_close_cents, cd.yes_ask_close_cents
          from market_candles_1m cd
         where cd.market_ticker = mk.ticker
           and cd.bucket_time <= mk.close_time - make_interval(mins => cps.minutes_before)
         order by cd.bucket_time desc
         limit 1
  ) c on true;

comment on view v_hourly_ask_ladder is
  'Touch at T-60/-45/-30/-15/-10/-5/-1 per market. T-60 must agree with '
  'market_snapshots where both exist (CP5).';


-- ----------------------------------------------------- v_hourly_capacity
-- An UPPER BOUND on fillable size, not a fill model.
--
-- Contracts that actually traded at or below a checkpoint's ask in the ten
-- minutes after it. It is an upper bound because those trades include fills we
-- would have been competing with, not just ones we could have taken; phase 1
-- cannot do better, because candles carry the touch and not the displayed
-- depth behind it. Only phase-2 orderbook snapshots settle this.
create or replace view v_hourly_capacity as
select l.ticker,
       l.series_ticker,
       l.close_time,
       l.minutes_before,
       l.yes_ask_close_cents as ask_cents,
       count(tr.kalshi_trade_id)                  as fills_after,
       coalesce(sum(tr."count"), 0)               as contracts_at_or_below_ask
  from v_hourly_ask_ladder l
  left join market_trades tr
         on tr.market_ticker = l.ticker
        and l.yes_ask_close_cents is not null
        and tr.trade_time >  l.close_time - make_interval(mins => l.minutes_before)
        and tr.trade_time <= l.close_time - make_interval(mins => l.minutes_before)
                             + interval '10 minutes'
        and tr.price_cents <= l.yes_ask_close_cents
 group by 1, 2, 3, 4, 5;

comment on view v_hourly_capacity is
  'Upper bound on fillable size per checkpoint: contracts traded at or below '
  'the checkpoint ask in the following 10 minutes.';
