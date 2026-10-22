"""Job B — settled sweep. Cron: 30 6 * * *  (build-spec §6)

Pages newly-settled markets since the last high-water mark, upserts them into
markets_terminal, then aggregates each tape into market_taker_stats.

D7 removed this job's main cost risk: `min_settled_ts` is a server-side filter
and markets carry `settlement_ts`, so the high-water mark is a query parameter
rather than a client-side scan of every settled market ever listed.

Hardening (Oct 22 2026, after the Oct 19 hang): on Oct 19 at 2:57 PM CT the
database restarted mid-run. The pooler kept the client socket alive, so the
keepalives from #3 never fired and the job blocked on a read for three days.
Railway reported the deployment as Running, and with a cron service that means
every later tick was skipped. This job now:

  * works in settlement-time chunks (SWEEP_CHUNK_HOURS, default 12) using
    min_settled_ts/max_settled_ts, and advances job_state.settled_sweep to the
    end of each chunk as soon as it completes, so a crash resumes from the last
    finished chunk instead of from the start of the backlog;
  * reconnects and retries the current chunk on psycopg OperationalError /
    InterfaceError (all writes are idempotent upserts, G3);
  * connects with tcp_user_timeout, keepalives and a client statement_timeout
    (sweep only; other jobs keep lib.db.CONNECT_KWARGS unchanged);
  * runs a watchdog thread that exits non-zero if nothing has progressed for
    SWEEP_STALL_MINUTES (default 20) or the run exceeds SWEEP_MAX_RUNTIME_HOURS
    (default 22), so a dead run fails loudly and never blocks the next cron tick.

Everything else (what is ingested, the volume gates, the tape aggregation, the
2 h overlap, the final cursor = run start) is unchanged.

Dry run (no DB at all, public API only):
    python jobs/settled_sweep.py --dry-run --since 2026-10-20T00:00:00Z --chunks 1
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import psycopg  # noqa: E402

from lib import db, mappers, tape  # noqa: E402
from lib.kalshi import KalshiClient  # noqa: E402

log = logging.getLogger("settled_sweep")
JOB = "settled_sweep"
BATCH = 200
LOOKBACK_DEFAULT_DAYS = 3  # first run with no cursor
TERMINAL_MIN_VOLUME = float(os.environ.get("TERMINAL_MIN_VOLUME", "1"))
OVERLAP = timedelta(hours=2)  # re-scan a little; upserts make it free (G3)

CHUNK = timedelta(hours=float(os.environ.get("SWEEP_CHUNK_HOURS", "12")))
STALL_SECONDS = float(os.environ.get("SWEEP_STALL_MINUTES", "20")) * 60
MAX_RUNTIME_SECONDS = float(os.environ.get("SWEEP_MAX_RUNTIME_HOURS", "22")) * 3600
DB_RETRIES = int(os.environ.get("SWEEP_DB_RETRIES", "5"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("SWEEP_STATEMENT_TIMEOUT_MS", "120000"))

# Sweep-only connection settings, merged over lib.db.CONNECT_KWARGS.
# tcp_user_timeout (ms) bounds how long sent data may stay unacknowledged, which
# keepalives alone do not cover once a request is in flight.
SWEEP_CONNECT_KWARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
    "tcp_user_timeout": 60_000,
}

DB_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)


# ------------------------------------------------------------------ watchdog

class Watchdog:
    """Exit the whole process if the run stalls or overruns.

    The main thread can be blocked inside a socket read that no Python-level
    timeout can interrupt, so the only reliable way out is os._exit from a
    side thread. Exit codes: 3 = stalled, 4 = max runtime exceeded.
    """

    def __init__(self, stall_s: float, max_runtime_s: float, check_every_s: float = 30.0,
                 clock=time.monotonic, exit_fn=os._exit):
        self.stall_s = stall_s
        self.max_runtime_s = max_runtime_s
        self.check_every_s = check_every_s
        self.clock = clock
        self.exit_fn = exit_fn
        self.started = clock()
        self.last_progress = self.started
        self.last_what = "start"
        self._stop = threading.Event()

    def touch(self, what: str = "") -> None:
        self.last_progress = self.clock()
        if what:
            self.last_what = what

    def check(self) -> int | None:
        now = self.clock()
        if now - self.started > self.max_runtime_s:
            log.error("WATCHDOG: max runtime %.1fh exceeded (last progress: %s); exiting 4",
                      self.max_runtime_s / 3600, self.last_what)
            return 4
        if now - self.last_progress > self.stall_s:
            log.error("WATCHDOG: no progress for %.0f min (last: %s); exiting 3",
                      (now - self.last_progress) / 60, self.last_what)
            return 3
        return None

    def _run(self) -> None:
        while not self._stop.wait(self.check_every_s):
            code = self.check()
            if code is not None:
                for h in logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
                self.exit_fn(code)
                return

    def start(self) -> "Watchdog":
        threading.Thread(target=self._run, name="watchdog", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


class _NullWatchdog:
    def touch(self, what: str = "") -> None:
        pass


WATCHDOG: Watchdog | _NullWatchdog = _NullWatchdog()


class ProgressClient(KalshiClient):
    """KalshiClient that tells the watchdog every successful request is progress."""

    def get(self, path: str, params: dict | None = None) -> dict:
        body = super().get(path, params)
        WATCHDOG.touch()
        return body


# ------------------------------------------------------------- connections

def sweep_connect_kwargs(dsn: str, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS) -> dict:
    """Connection kwargs for the sweep, without clobbering DSN `options`.

    An explicit `options=` kwarg replaces any `options` already in the DSN
    (some pooler URLs carry routing info there), so append to it instead.
    """
    from psycopg.conninfo import conninfo_to_dict

    kwargs = dict(SWEEP_CONNECT_KWARGS)
    if statement_timeout_ms > 0:
        existing = (conninfo_to_dict(dsn).get("options") or "").strip()
        st = f"-c statement_timeout={statement_timeout_ms}"
        kwargs["options"] = f"{existing} {st}".strip()
    return kwargs


class Conn:
    """A db.connect() context we can drop and reopen mid-run."""

    def __init__(self):
        self._cm = None
        self.conn = None
        self.use_options = True

    def open(self):
        extra = sweep_connect_kwargs(db.dsn(), STATEMENT_TIMEOUT_MS if self.use_options else 0)
        try:
            cm = db.connect(extra_kwargs=extra)
            conn = cm.__enter__()
        except psycopg.OperationalError as exc:
            msg = str(exc).lower()
            if self.use_options and ("options" in msg or "startup parameter" in msg):
                log.warning("server rejected startup options; reconnecting without the "
                            "client statement_timeout (server default still applies)")
                self.use_options = False
                return self.open()
            raise
        self._cm, self.conn = cm, conn
        log.info("db connected (tcp_user_timeout=%dms, client statement_timeout=%s)",
                 SWEEP_CONNECT_KWARGS["tcp_user_timeout"],
                 f"{STATEMENT_TIMEOUT_MS}ms" if self.use_options else "server default")
        return conn

    def close(self, exc: BaseException | None = None) -> None:
        cm, self._cm, self.conn = self._cm, None, None
        if cm is None:
            return
        try:
            if exc is None:
                cm.__exit__(None, None, None)
            else:
                cm.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as close_exc:  # dead socket: rollback/close may fail too
            log.debug("ignoring error while closing connection: %s", close_exc)


# --------------------------------------------------------------- the sweep

def plan_chunks(since: datetime, until: datetime, chunk: timedelta = CHUNK
                ) -> list[tuple[datetime, datetime]]:
    """Split [since, until] into consecutive settlement-time windows."""
    if chunk <= timedelta(0):
        raise ValueError("chunk must be positive")
    out = []
    lo = since
    while lo < until:
        hi = min(lo + chunk, until)
        out.append((lo, hi))
        lo = hi
    return out or [(since, until)]


def event_series_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("select event_ticker, series_ticker from events where series_ticker is not null")
        return dict(cur.fetchall())


def sweep_chunk(conn, client: KalshiClient, lo: datetime, hi: datetime, page_cap: int,
                done: set[str], label: str = "") -> tuple[int, int]:
    """One settlement-time window. Same logic as the original single-pass sweep."""
    ev_map = event_series_map(conn)
    fee_mults = tape.fee_multipliers(conn)
    WATCHDOG.touch(f"{label} loaded maps")

    markets, batch, ingested, skipped_novol = [], [], 0, 0

    for m in client.iter_markets(status="settled", min_settled_ts=int(lo.timestamp()),
                                 max_settled_ts=int(hi.timestamp())):
        if m.get("status") not in mappers.TERMINAL_STATUSES:  # D4
            continue
        st = ev_map.get(m.get("event_ticker"))
        vol = float(m.get("volume_fp") or 0)

        # Storage lever, default ON (see README > Storage).
        if vol < TERMINAL_MIN_VOLUME:
            skipped_novol += 1
            continue

        batch.append(mappers.terminal_row(m, st))
        ingested += 1

        if vol >= tape.MIN_VOLUME:
            markets.append({"ticker": m["ticker"], "result": m.get("result"),
                            "series_ticker": st,
                            "event_ticker": m.get("event_ticker")})

        if len(batch) >= BATCH:
            db.upsert(conn, "markets_terminal", batch, conflict="ticker")
            conn.commit()
            WATCHDOG.touch(f"{label} terminal {ingested}")
            batch = []
    if batch:
        db.upsert(conn, "markets_terminal", batch, conflict="ticker")
        conn.commit()

    unlinked = {m["event_ticker"] for m in markets
                if m["event_ticker"] and not m["series_ticker"]}
    tape.resolve_missing_events(conn, client, unlinked)
    conn.commit()
    repaired = tape.repair_series_links(conn)
    conn.commit()
    WATCHDOG.touch(f"{label} events resolved")

    if repaired:
        ev_map = event_series_map(conn)
        for m in markets:
            if not m["series_ticker"]:
                m["series_ticker"] = ev_map.get(m["event_ticker"])

    todo = [m for m in markets if m["ticker"] not in done]
    log.info("%s terminal ingested: %d (%d skipped below TERMINAL_MIN_VOLUME=%s, "
             "tape queue %d, %d already aggregated, %d to aggregate, %d series links repaired)",
             label, ingested, skipped_novol, TERMINAL_MIN_VOLUME, len(markets),
             len(markets) - len(todo), len(todo), repaired)

    aggregated = 0
    pending: list[str] = []
    for m in todo:
        st = tape.process_market(conn, client, m, fee_mults, page_cap)
        aggregated += 1
        pending.append(m["ticker"])
        WATCHDOG.touch(f"{label} aggregated {m['ticker']}")
        if aggregated % 25 == 0:
            conn.commit()
            done.update(pending)
            pending = []
            log.info("%s aggregated %d/%d (last: %s, %d trades)",
                     label, aggregated, len(todo), m["ticker"], st.trades)
    conn.commit()
    done.update(pending)
    return ingested, aggregated


def run(page_cap: int) -> int:
    client = ProgressClient()
    started = datetime.now(timezone.utc)
    c = Conn()

    conn = c.open()
    try:
        cursor_ts, _ = db.get_job_cursor(conn, JOB)
        since = (cursor_ts - OVERLAP) if cursor_ts else started - timedelta(days=LOOKBACK_DEFAULT_DAYS)
        done = tape.existing_stats(conn)
    except Exception as exc:
        c.close(exc)
        raise

    chunks = plan_chunks(since, started)
    log.info("sweeping settlements since %s until %s in %d chunk(s) of <= %.1fh "
             "(%d tickers already aggregated)",
             since.isoformat(), started.isoformat(), len(chunks),
             CHUNK.total_seconds() / 3600, len(done))
    WATCHDOG.touch("planned")

    tot_markets = tot_agg = 0
    for i, (lo, hi) in enumerate(chunks, 1):
        label = f"[chunk {i}/{len(chunks)} {lo:%m-%d %H:%M}Z..{hi:%m-%d %H:%M}Z]"
        t0 = time.monotonic()
        for attempt in range(1, DB_RETRIES + 1):
            try:
                if c.conn is None:
                    c.open()
                n_markets, n_agg = sweep_chunk(c.conn, client, lo, hi, page_cap, done, label)
                db.set_job_cursor(c.conn, JOB, cursor_ts=hi,
                                  notes={"markets": n_markets, "aggregated": n_agg,
                                         "chunk": i, "chunks": len(chunks),
                                         "chunk_from": lo.isoformat(),
                                         "run_started": started.isoformat()})
                c.conn.commit()
                break
            except DB_ERRORS as exc:
                log.warning("%s database error (attempt %d/%d): %s: %s",
                            label, attempt, DB_RETRIES, type(exc).__name__, exc)
                c.close(exc)
                if attempt == DB_RETRIES:
                    raise
                time.sleep(min(10 * 2 ** (attempt - 1), 120))
                WATCHDOG.touch(f"{label} reconnecting")
            except Exception as exc:
                c.close(exc)
                raise
        tot_markets += n_markets
        tot_agg += n_agg
        log.info("%s done: %d markets, %d aggregated in %.1f min; cursor -> %s",
                 label, n_markets, n_agg, (time.monotonic() - t0) / 60, hi.isoformat())

    c.close()
    log.info("done: %d markets, %d aggregated, %d requests",
             tot_markets, tot_agg, client.request_count)
    return 0


def dry_run(since: datetime, n_chunks: int, max_pages: int | None) -> int:
    """Public API only, no DB: show what each chunk would ingest."""
    client = KalshiClient()
    until = datetime.now(timezone.utc)
    for i, (lo, hi) in enumerate(plan_chunks(since, until)[:n_chunks], 1):
        n = vol_ok = tape_ok = 0
        for m in client.iter_markets(status="settled", min_settled_ts=int(lo.timestamp()),
                                     max_settled_ts=int(hi.timestamp()), max_pages=max_pages):
            if m.get("status") not in mappers.TERMINAL_STATUSES:
                continue
            n += 1
            vol = float(m.get("volume_fp") or 0)
            vol_ok += vol >= TERMINAL_MIN_VOLUME
            tape_ok += vol >= tape.MIN_VOLUME
            mappers.terminal_row(m, None)  # exercises the mapper
        log.info("DRY RUN chunk %d %s..%s: %d settled, %d >= TERMINAL_MIN_VOLUME, %d tape-eligible, "
                 "%d requests%s", i, lo.isoformat(), hi.isoformat(), n, vol_ok, tape_ok,
                 client.request_count, f" (capped at {max_pages} pages)" if max_pages else "")
    return 0


def main(argv: list[str] | None = None) -> int:
    global WATCHDOG
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="public API only, no DB")
    ap.add_argument("--since", help="dry run: ISO start (default now - 12h)")
    ap.add_argument("--chunks", type=int, default=1)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args(argv)

    if args.dry_run:
        since = (datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since
                 else datetime.now(timezone.utc) - CHUNK)
        return dry_run(since, args.chunks, args.max_pages)

    page_cap = int(os.environ.get("TAPE_PAGE_CAP", tape.DEFAULT_PAGE_CAP))
    WATCHDOG = Watchdog(STALL_SECONDS, MAX_RUNTIME_SECONDS).start()
    log.info("watchdog: stall %.0f min, max runtime %.1fh; chunk %.1fh; db retries %d",
             STALL_SECONDS / 60, MAX_RUNTIME_SECONDS / 3600, CHUNK.total_seconds() / 3600,
             DB_RETRIES)
    try:
        return run(page_cap)
    finally:
        WATCHDOG.stop()


if __name__ == "__main__":
    raise SystemExit(main())
