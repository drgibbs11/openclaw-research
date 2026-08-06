-- Kalshi long-tail screener — schema
-- Deviations from build-spec §5 are all traceable to DISCREPANCIES.md (D1-D20).
-- G5: every timestamp is timestamptz, stored UTC.
-- G4: entity tables keep the full source object in `raw`. (Exception: D20.)

-- ---------------------------------------------------------------- series
create table if not exists series (
  series_ticker   text primary key,          -- source field is `ticker` (D5)
  title           text,
  category        text,
  frequency       text,                      -- real enum, drives recurrence (D9)
  tags            jsonb,
  settlement_sources jsonb,
  contract_url    text,
  contract_terms_url text,
  fee_type        text,                      -- quadratic | quadratic_with_maker_fees (D10)
  fee_multiplier  numeric,                   -- 0 or 1; 0 => no fees (D10)
  first_seen      timestamptz not null default now(),
  last_seen       timestamptz,
  raw             jsonb not null
);

-- ---------------------------------------------------------------- events
create table if not exists events (
  event_ticker    text primary key,
  series_ticker   text,                      -- present on the event object (D14)
  title           text,
  category        text,
  mutually_exclusive boolean,
  settlement_sources jsonb,
  raw             jsonb not null,
  last_seen       timestamptz
);
create index if not exists ev_series on events (series_ticker);

-- ---------------------------------------------------------------- snapshots
-- Append-only; one row per open market per job run. No `raw` column by
-- deliberate exception to G4 — see D20.
-- All prices are dollars (numeric), not integer cents (D1). All counts are
-- numeric because contracts are fractional (D2).
create table if not exists market_snapshots (
  ticker          text not null,
  run_ts          timestamptz not null,
  series_ticker   text,
  event_ticker    text,
  status          text,
  yes_bid         numeric,                   -- yes_bid_dollars
  yes_ask         numeric,                   -- yes_ask_dollars
  no_bid          numeric,
  no_ask          numeric,
  last_price      numeric,
  volume          numeric,                   -- volume_fp, cumulative since listing
  volume_24h      numeric,                   -- volume_24h_fp, the activity field
  open_interest   numeric,                   -- open_interest_fp
  liquidity       numeric,                   -- liquidity_dollars
  price_level_structure text,                -- cent | deci_cent (D3)
  close_time      timestamptz,
  primary key (ticker, run_ts)
);
create index if not exists ms_series_ts on market_snapshots (series_ticker, run_ts);
create index if not exists ms_run_ts on market_snapshots (run_ts);

-- ---------------------------------------------------------------- terminal
create table if not exists markets_terminal (
  ticker          text primary key,
  series_ticker   text,
  event_ticker    text,
  title           text,
  status          text,                      -- settled | finalized (D4)
  result          text,                      -- expect 'yes'/'no'; keep others, exclude in views
  open_time       timestamptz,
  close_time      timestamptz,
  settlement_ts   timestamptz,               -- Job B high-water mark field (D7)
  volume          numeric,
  open_interest   numeric,
  price_level_structure text,
  rules_primary   text,
  raw             jsonb not null,
  ingested_at     timestamptz not null default now()
);
create index if not exists mt_series on markets_terminal (series_ticker);
create index if not exists mt_settled on markets_terminal (settlement_ts);
create index if not exists mt_volume on markets_terminal (volume);

-- ---------------------------------------------------------------- tape stats
-- Aggregates computed while paging the tape; raw trades are NOT stored (§12).
-- PnL is accumulated in cents/contract terms for readability even though the
-- wire format is dollars (D1).
create table if not exists market_taker_stats (
  ticker          text primary key references markets_terminal(ticker),
  trades          int,
  contracts       numeric,
  taker_yes_contracts numeric,
  taker_yes_vwap_cents numeric,
  taker_no_contracts  numeric,
  taker_no_vwap_cents  numeric,
  taker_gross_pnl_cents numeric,   -- settlement-referenced, §7. negative = takers bled
  taker_fee_est_cents   numeric,   -- POSITIVE magnitude of estimated cost, §7 + D10
  block_trades_skipped  int not null default 0,   -- D11
  pages_read      int,
  truncated       boolean not null default false,
  computed_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------- tags
create table if not exists series_tags (
  series_ticker   text primary key references series(series_ticker),
  settlement_source_type text, -- scrapable_numeric_feed|official_report|committee_or_subjective|market_price|unknown
  benchmark       text,        -- none|sportsbook|cme_or_rates|spot_crypto|other_liquid_market|unknown
  benchmark_name  text,
  recurrence      text,        -- DERIVED from series.frequency, not from the model (D9)
  scrape_difficulty text,      -- low|medium|high|unknown
  source_urls     jsonb,
  notes           text,
  model           text,
  classified_at   timestamptz default now(),
  reviewed        boolean not null default false
);

-- ---------------------------------------------------------------- job state
-- Referenced by build-spec §6 but never defined there (D19). Keyed rather than
-- one-row so Jobs B and C keep independent cursors.
create table if not exists job_state (
  job_name        text primary key,
  cursor_ts       timestamptz,
  cursor_text     text,
  updated_at      timestamptz not null default now(),
  notes           jsonb
);
