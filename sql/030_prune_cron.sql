-- Snapshot retention as pg_cron jobs. This is the deployed retention path.
--
-- market_snapshots is the only append-only screener table that grows without
-- bound: ~78k open markets x 4 runs/day x ~296 bytes = ~92 MB/day.
-- v_series_activity reads only the last 24 hours; the rest is history. A
-- 30-day window caps the table at roughly 2.6 GB.
--
-- Why in-database rather than a Railway worker:
--
--   VACUUM cannot run over PostgREST at all, and behaves badly through
--   Supabase's transaction pooler. An external worker therefore either skips
--   it -- leaving the heap at full size however much the DELETE removed --
--   or needs its own direct connection purely for one maintenance command.
--   pg_cron runs in-process and can do both.
--
-- Two jobs rather than one: VACUUM cannot run inside a transaction block, so
-- it cannot be chained onto the DELETE in a single command string. The vacuum
-- is scheduled 20 minutes after the delete to let it finish first.
--
-- Verified rather than assumed: a probe job scheduled two minutes out returned
-- status = succeeded, return_message = VACUUM in cron.job_run_details.
--
-- Applied to the live database as migration `screener_prune_via_pg_cron`.
-- Requires the pg_cron extension (present on Supabase) and a role that may
-- schedule jobs (postgres).

select cron.schedule(
  'screener-prune-snapshots',
  '0 5 * * *',
  $$delete from screener.market_snapshots
     where run_ts < now() - interval '30 days'$$
);

select cron.schedule(
  'screener-vacuum-snapshots',
  '20 5 * * *',
  $$vacuum (analyze) screener.market_snapshots$$
);

-- Inspect:
--   select jobid, jobname, schedule, active from cron.job;
--   select j.jobname, d.status, d.return_message, d.start_time
--     from cron.job_run_details d join cron.job j using (jobid)
--    where j.jobname like 'screener-%'
--    order by d.start_time desc limit 10;
--
-- Remove:
--   select cron.unschedule('screener-prune-snapshots');
--   select cron.unschedule('screener-vacuum-snapshots');
