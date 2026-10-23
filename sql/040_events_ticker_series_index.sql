-- 2026-10-23 perf fix: covering partial index for event_ticker -> series_ticker lookups.
-- Lets jobs/snapshot.py's per-page lookup (event_ticker = any(...)) and the full-map scans in
-- settled_sweep.py / backfill.py run as index-only scans instead of reading the 1.5 GB heap.
-- Applied on production 2026-10-23. CONCURRENTLY: must run OUTSIDE a transaction block
-- (psql autocommit; not via lib.db.apply_sql_file).
-- Rollback: drop index concurrently if exists screener.ev_ticker_series_cov;
create index concurrently if not exists ev_ticker_series_cov
  on screener.events (event_ticker) include (series_ticker)
  where series_ticker is not null;
