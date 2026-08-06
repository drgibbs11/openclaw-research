-- Retention. Run daily (Railway cron, alongside Job B).
--
-- market_snapshots is the only append-only table that grows without bound:
-- ~78k open markets x 4 runs/day x ~296 bytes = ~92 MB/day, ~2.8 GB/month.
-- v_series_activity only reads the last 24 hours of it, so the rest is kept
-- purely as history. Keeping 30 days caps the table at roughly 2.8 GB; keeping
-- 7 days caps it at roughly 650 MB.
--
-- Everything else is bounded already: series and events are upsert-in-place,
-- markets_terminal and market_taker_stats grow with settlements (~150 MB/day
-- at current listing rates — see README for how to bound that at ingest time,
-- which is cheaper than deleting afterwards).
--
-- Tune with :days, e.g.  psql "$DATABASE_URL" -v days=7 -f sql/020_prune.sql
\if :{?days}
\else
  \set days 30
\endif

begin;

delete from market_snapshots
 where run_ts < now() - (:'days' || ' days')::interval;

-- Reclaim to the OS rather than leaving it as free space in the heap. A plain
-- DELETE only marks tuples dead; on a table this size that is the difference
-- between a stable footprint and one that only ever grows.
commit;

vacuum (analyze) market_snapshots;
