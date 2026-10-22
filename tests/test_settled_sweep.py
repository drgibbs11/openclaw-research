"""Offline tests for the hardened settled sweep. No DB, no network.

    python -m pytest tests/test_settled_sweep.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "jobs")

import settled_sweep as ss  # noqa: E402
from lib import db  # noqa: E402

UTC = timezone.utc


def test_plan_chunks_covers_window_contiguously():
    since = datetime(2026, 10, 17, 4, 33, tzinfo=UTC)
    until = datetime(2026, 10, 22, 19, 0, tzinfo=UTC)
    chunks = ss.plan_chunks(since, until, timedelta(hours=12))
    assert chunks[0][0] == since and chunks[-1][1] == until
    for (a, b), (c, d) in zip(chunks, chunks[1:]):
        assert b == c and b - a == timedelta(hours=12)
    assert len(chunks) == 12


def test_plan_chunks_empty_window_still_one_chunk():
    t = datetime(2026, 10, 22, tzinfo=UTC)
    assert ss.plan_chunks(t, t) == [(t, t)]


def test_watchdog_stall_and_runtime():
    now = [0.0]
    wd = ss.Watchdog(stall_s=60, max_runtime_s=600, clock=lambda: now[0])
    now[0] = 59
    assert wd.check() is None
    now[0] = 61
    assert wd.check() == 3
    wd.touch("x")
    assert wd.check() is None
    now[0] = 601
    wd.touch("y")
    assert wd.check() == 4


def test_connect_kwargs_unchanged_for_other_jobs():
    assert db.CONNECT_KWARGS == {"connect_timeout": 15, "keepalives": 1, "keepalives_idle": 30,
                                 "keepalives_interval": 10, "keepalives_count": 5}


def test_sweep_kwargs_append_to_existing_dsn_options():
    k = ss.sweep_connect_kwargs("postgresql://u@h:6543/d?options=reference%3Dabc", 120000)
    assert k["options"] == "reference=abc -c statement_timeout=120000"
    assert k["tcp_user_timeout"] == 60000
    k2 = ss.sweep_connect_kwargs("postgresql://u@h:5432/d", 0)
    assert "options" not in k2


def test_libpq_accepts_sweep_kwargs():
    # Port 9 on localhost refuses; an unknown keyword would fail differently.
    kwargs = {**db.CONNECT_KWARGS, **ss.sweep_connect_kwargs("postgresql://u@127.0.0.1:9/d")}
    kwargs["connect_timeout"] = 2
    with pytest.raises(psycopg.OperationalError) as ei:
        psycopg.connect("postgresql://u@127.0.0.1:9/d", **kwargs)
    assert "invalid" not in str(ei.value).lower()


# ------------------------------------------------------------ fakes

class FakeCur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql = sql
    def fetchall(self): return [("EV1", "SER1")]


class FakeConn:
    def __init__(self): self.commits = 0
    def cursor(self): return FakeCur(self)
    def commit(self): self.commits += 1


class FakeClient:
    def __init__(self, markets): self.markets = markets; self.calls = []
    def iter_markets(self, **kw):
        self.calls.append(kw)
        lo, hi = kw["min_settled_ts"], kw["max_settled_ts"]
        return iter([m for m in self.markets if lo <= m["_ts"] <= hi])


def _mk(t, ts, vol="5"):
    return {"ticker": t, "event_ticker": "EV1", "status": "finalized", "result": "yes",
            "volume_fp": vol, "_ts": ts}


def test_sweep_chunk_filters_window_and_skips_done(monkeypatch):
    written, processed = [], []
    monkeypatch.setattr(ss.db, "upsert", lambda conn, table, rows, conflict: written.extend(r["ticker"] for r in rows) or len(rows))
    monkeypatch.setattr(ss.mappers, "terminal_row", lambda m, st: {"ticker": m["ticker"], "series": st})
    monkeypatch.setattr(ss.tape, "fee_multipliers", lambda conn: {})
    monkeypatch.setattr(ss.tape, "resolve_missing_events", lambda *a: 0)
    monkeypatch.setattr(ss.tape, "repair_series_links", lambda conn: 0)

    class St: trades = 1
    monkeypatch.setattr(ss.tape, "process_market",
                        lambda conn, client, m, fm, cap: processed.append(m["ticker"]) or St())
    lo = datetime(2026, 10, 20, tzinfo=UTC)
    hi = lo + timedelta(hours=12)
    mk = [_mk("A", int(lo.timestamp()) + 10), _mk("B", int(lo.timestamp()) + 20),
          _mk("Z", int(lo.timestamp()) + 30, vol="0"), _mk("LATE", int(hi.timestamp()) + 99)]
    done = {"A"}
    n, agg = ss.sweep_chunk(FakeConn(), FakeClient(mk), lo, hi, 50, done, "[t]")
    assert written == ["A", "B"]          # Z below volume gate, LATE outside window
    assert processed == ["B"] and agg == 1 and n == 2
    assert done == {"A", "B"}


def test_run_reconnects_and_advances_cursor_per_chunk(monkeypatch):
    cursor = datetime.now(UTC) - timedelta(hours=30)
    set_calls, attempts = [], {"n": 0}
    opened = []

    class FakeConnObj:
        def __init__(self): self.conn = None
        def open(self):
            self.conn = FakeConn(); opened.append(1); return self.conn
        def close(self, exc=None): self.conn = None

    monkeypatch.setattr(ss, "Conn", FakeConnObj)
    monkeypatch.setattr(ss, "ProgressClient", lambda: type("C", (), {"request_count": 0})())
    monkeypatch.setattr(ss.db, "get_job_cursor", lambda conn, job: (cursor, None))
    monkeypatch.setattr(ss.tape, "existing_stats", lambda conn: set())
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss.db, "set_job_cursor",
                        lambda conn, job, cursor_ts=None, notes=None: set_calls.append(cursor_ts))

    def chunk(conn, client, lo, hi, cap, done, label):
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return 1, 1
    monkeypatch.setattr(ss, "sweep_chunk", chunk)

    assert ss.run(50) == 0
    # since = cursor - 2h -> 32h window -> 3 chunks; chunk 2 retried once after reconnect
    assert len(set_calls) == 3 and attempts["n"] == 4 and len(opened) == 2
    assert set_calls == sorted(set_calls) and set_calls[0] == cursor - timedelta(hours=2) + timedelta(hours=12)
