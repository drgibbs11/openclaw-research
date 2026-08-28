-- Hourly temperature markets (KXTEMP*H) — phase 1 backfill, tables and gates.
--
-- Two new tables and one flag. Everything else this phase needs already exists:
-- markets_terminal holds the settled ladders, market_snapshots already captures
-- the T-60 opening book of every ladder that opens at 00/06/12/18Z, and
-- market_taker_stats holds the per-ticker aggregate this tape must reproduce.
--
-- Nothing here touches the mlb_* tables, loaders or modules. There is no
-- dependency on them in any direction.

set search_path to screener;

-- ------------------------------------------------------------------ gate
-- Which series persist their per-trade tape. A column on `series` rather than
-- a new config table: the tape reader already loads `series` for fee
-- multipliers, so the flag rides along on a query that was happening anyway.
alter table series add column if not exists persist_trades boolean not null default false;

comment on column series.persist_trades is
  'Persist the per-trade tape to market_trades, not just the aggregate in '
  'market_taker_stats. Off by default: the aggregate is enough to screen a '
  'series, and the tape is ~8k rows per series-day.';

-- The seven hourly temperature series. BOS has no settled markets yet; it is
-- flagged anyway so its tape starts accumulating the day it lists, rather than
-- being discovered missing later.
update series set persist_trades = true
 where series_ticker in ('KXTEMPNYCH','KXTEMPAUSH','KXTEMPCHIH','KXTEMPDCH',
                         'KXTEMPLAXH','KXTEMPMIAH','KXTEMPBOSH');

-- ------------------------------------------------------------ per-trade tape
-- One row per public tape fill. Block trades are excluded upstream (D11) and
-- never reach this table.
--
-- price_cents is the YES price. It is NUMERIC, not INT, deliberately: 1,099
-- NYC markets between 2026-03-24 and 2026-03-30 are `tapered_deci_cent` and
-- quote in tenths, so an integer column would silently round 1.6% of the tape
-- and put the CP4 PnL reproduction permanently out of tolerance on exactly
-- those tickers. Numeric is strictly wider — a reader expecting whole cents
-- still gets whole cents everywhere else. See D37.
create table if not exists market_trades (
  kalshi_trade_id text primary key,
  market_ticker   text not null,
  series_ticker   text,
  trade_time      timestamptz not null,
  price_cents     numeric not null,
  count           numeric not null,
  taker_side      text not null check (taker_side in ('yes','no')),
  ingested_at     timestamptz not null default now()
);
create index if not exists mtr_ticker_time on market_trades (market_ticker, trade_time);
create index if not exists mtr_series_time on market_trades (series_ticker, trade_time);

comment on table market_trades is
  'Per-trade public tape for series with persist_trades. Feeds '
  'v_hourly_bleed_by_minute; must reproduce market_taker_stats (CP4).';

-- --------------------------------------------------------------- 1m candles
-- bucket_time is the INCLUSIVE PERIOD END, matching the API''s end_period_ts.
-- Naming it bucket_time rather than end_ts keeps it aligned with every other
-- bucketed table here, but the semantics are the end, not the start: a row at
-- 05:00:00Z covers 04:59:00–05:00:00.
--
-- Cents columns are numeric for the same deci-cent reason as market_trades.
create table if not exists market_candles_1m (
  market_ticker       text not null,
  bucket_time         timestamptz not null,
  yes_bid_open_cents  numeric,
  yes_bid_close_cents numeric,
  yes_ask_open_cents  numeric,
  yes_ask_close_cents numeric,
  price_open_cents    numeric,
  price_high_cents    numeric,
  price_low_cents     numeric,
  price_close_cents   numeric,
  volume              numeric,
  open_interest       numeric,
  primary key (market_ticker, bucket_time)
);
create index if not exists mc1_time on market_candles_1m (bucket_time);

comment on table market_candles_1m is
  'Change-only 1-minute candles over each hourly market''s 60-minute life. '
  'bucket_time is the inclusive period END.';

-- ------------------------------------------------------------------ retention
-- NOT registered in public.retention_policy, deliberately.
--
-- public.downsample_table interpolates the policy''s table_name with format()
-- %I, which quotes the whole string as ONE identifier: a row naming
-- 'screener.market_trades' becomes "screener.market_trades", a table of that
-- literal name, which does not exist. The function also pins its search_path
-- to pg_catalog,public, so an unqualified screener table cannot resolve
-- either. public.run_retention wraps each table in its own exception block, so
-- such a row would not break retention for the live weather tables — it would
-- simply report an error every night forever while pruning nothing.
--
-- Screener retention therefore stays where the screener''s existing retention
-- already lives: pg_cron, alongside screener-prune-snapshots (sql/030).
-- Windows are the ones the spec asked for.
--
-- Apply once, as a migration:
--
--   select cron.schedule('screener-prune-hourly-trades', '10 5 * * *',
--     $$delete from screener.market_trades
--        where trade_time < now() - interval '365 days'$$);
--
--   select cron.schedule('screener-prune-hourly-candles', '15 5 * * *',
--     $$delete from screener.market_candles_1m
--        where bucket_time < now() - interval '365 days'$$);
--
--   select cron.schedule('screener-vacuum-hourly', '35 5 * * *',
--     $$vacuum (analyze) screener.market_trades, screener.market_candles_1m$$);
--
-- The 120-day trades / 60-day candles full-resolution horizons in the spec
-- describe downsampling, not deletion. Neither table is downsampled yet: a
-- trade is a discrete event and cannot be bucketed without destroying the
-- minute-level bleed map that is the whole point of collecting it, and the
-- candles are already the bucketed form. Revisit when the tables are large
-- enough to matter — at the projected ~560k + ~350k rows they are not.
