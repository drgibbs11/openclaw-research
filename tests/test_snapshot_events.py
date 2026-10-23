"""Tests for the 2026-10-23 events upsert fix (lib/bulk.py + jobs/snapshot.py).

Offline tests need no DB. The Postgres tests run only when SCREENER_TEST_PG is set
to a DSN of a *throwaway* database (they create and drop their own schema):

    SCREENER_TEST_PG=postgresql://... python -m pytest tests -q
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "jobs")

import snapshot  # noqa: E402
from lib import bulk, mappers  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def ev(t, series="S1", title="T", **kw):
    return {"event_ticker": t, "series_ticker": series, "title": title, "category": "c",
            "mutually_exclusive": True, "settlement_sources": [{"name": "x"}], **kw}


# ------------------------------------------------------------------ offline

def test_compare_cols_cover_every_column_event_row_writes():
    cols = set(mappers.event_row(ev("E1"), NOW))
    assert set(snapshot.EVENT_COMPARE_COLS) == cols - {"event_ticker", "last_seen"}


def test_sql_has_change_filter_and_touch_interval():
    row = mappers.event_row(ev("E1"), NOW)
    q = bulk.build_upsert_sql("events", list(row), ["event_ticker"], 3,
                              snapshot.EVENT_COMPARE_COLS, "last_seen").as_string(None)
    assert q.count("(%s, %s, %s, %s, %s, %s, %s, %s)") == 3
    assert "is distinct from" in q and "'1 day'::interval" in q
    for c in snapshot.EVENT_COMPARE_COLS:
        assert f'"events"."{c}"' in q and f'excluded."{c}"' in q
    assert '"last_seen" = excluded."last_seen"' in q  # still written when the row updates


def test_compare_col_not_written_is_rejected():
    with pytest.raises(ValueError):
        bulk.build_upsert_sql("events", ["event_ticker", "title"], ["event_ticker"], 1, ["raw"], None)


def test_dedupe_keeps_last_row_per_key():
    out = bulk._dedupe([{"k": 1, "v": "a"}, {"k": 2, "v": "b"}, {"k": 1, "v": "c"}, {}], ["k"])
    assert sorted((r["k"], r["v"]) for r in out) == [(1, "c"), (2, "b")]


class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, q, params):
        self.conn.queries.append(params)
        self._res = [(t, self.conn.data[t]) for t in params[0] if self.conn.data.get(t)]
    def fetchall(self): return self._res


class _Conn:
    def __init__(self, data): self.data, self.queries = data, []
    def cursor(self): return _Cur(self)


def test_lookup_series_queries_only_new_tickers():
    conn = _Conn({"E1": "S1", "E2": "S2"})
    cache = {}
    snapshot.lookup_series(conn, ["E1", "E3", None, "E1"], cache)
    assert cache == {"E1": "S1", "E3": None} and conn.queries == [(["E1", "E3"],)]
    snapshot.lookup_series(conn, ["E1", "E3", "E2"], cache)
    assert conn.queries[-1] == (["E2"],) and cache["E2"] == "S2"
    snapshot.lookup_series(conn, ["E1"], cache)
    assert len(conn.queries) == 2  # fully cached: no query


# ------------------------------------------------------------------ postgres

PG = os.environ.get("SCREENER_TEST_PG")
needs_pg = pytest.mark.skipif(not PG, reason="SCREENER_TEST_PG not set")


@pytest.fixture
def pg():
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:8]
    conn = psycopg.connect(PG, prepare_threshold=None)
    conn.execute(f"create schema {schema}")
    conn.execute(f"set search_path to {schema}")
    conn.execute("""create table events (event_ticker text primary key, series_ticker text, title text,
        category text, mutually_exclusive boolean, settlement_sources jsonb, raw jsonb not null,
        last_seen timestamptz)""")
    conn.execute("create table market_snapshots (ticker text not null, run_ts timestamptz not null, "
                 "series_ticker text, event_ticker text, status text, yes_bid numeric, yes_ask numeric, "
                 "no_bid numeric, no_ask numeric, last_price numeric, volume numeric, volume_24h numeric, "
                 "open_interest numeric, liquidity numeric, price_level_structure text, close_time timestamptz, "
                 "primary key (ticker, run_ts))")
    conn.commit()
    yield conn
    conn.rollback()
    conn.execute(f"drop schema {schema} cascade")
    conn.commit()
    conn.close()


def _write(conn, events, now, page_size=500):
    rows = [mappers.event_row(e, now) for e in events]
    st = bulk.upsert_changed(conn, "events", rows, "event_ticker", snapshot.EVENT_COMPARE_COLS,
                             "last_seen", "1 day", page_size)
    conn.commit()
    return st


def _xmins(conn):
    return dict(conn.execute("select event_ticker, xmin::text from events").fetchall())


@needs_pg
def test_unchanged_rows_are_not_rewritten(pg):
    evs = [ev(f"E{i}") for i in range(1203)]
    assert _write(pg, evs, NOW, page_size=500) == {"seen": 1203, "inserted": 1203, "updated": 0, "unchanged": 0}
    before = _xmins(pg)
    st = _write(pg, evs, NOW + timedelta(hours=6), page_size=500)
    assert st == {"seen": 1203, "inserted": 0, "updated": 0, "unchanged": 1203}
    assert _xmins(pg) == before  # no new tuple versions at all
    assert pg.execute("select max(last_seen) from events").fetchone()[0] == NOW


@needs_pg
@pytest.mark.parametrize("field,value", [
    ("series_ticker", "S9"), ("series_ticker", None), ("title", "new"), ("category", None),
    ("mutually_exclusive", False), ("settlement_sources", [{"name": "y"}]), ("extra_raw_only", 1),
])
def test_any_changed_column_updates(pg, field, value):
    _write(pg, [ev("E1"), ev("E2")], NOW)
    changed = ev("E1"); changed[field] = value
    st = _write(pg, [changed, ev("E2")], NOW + timedelta(hours=6))
    assert st["updated"] == 1 and st["unchanged"] == 1
    row = pg.execute("select raw, last_seen from events where event_ticker='E1'").fetchone()
    assert row[0] == changed and row[1] == NOW + timedelta(hours=6)


@needs_pg
def test_last_seen_refreshed_after_a_day(pg):
    _write(pg, [ev("E1")], NOW)
    assert _write(pg, [ev("E1")], NOW + timedelta(hours=23))["updated"] == 0
    assert _write(pg, [ev("E1")], NOW + timedelta(hours=25))["updated"] == 1
    assert pg.execute("select last_seen from events").fetchone()[0] == NOW + timedelta(hours=25)


@needs_pg
def test_null_last_seen_is_refreshed(pg):
    _write(pg, [ev("E1")], NOW)
    pg.execute("update events set last_seen = null"); pg.commit()
    assert _write(pg, [ev("E1")], NOW)["updated"] == 1


@needs_pg
def test_duplicate_keys_in_one_batch_last_wins(pg):
    st = _write(pg, [ev("E1", title="a"), ev("E1", title="b")], NOW)
    assert st["seen"] == 1
    assert pg.execute("select title from events").fetchone()[0] == "b"


class FakeClient:
    request_count = 0

    def __init__(self, events, markets):
        self.events, self.markets = events, markets

    def iter_events(self):
        yield from self.events

    def iter_markets(self, status):
        yield from self.markets


@needs_pg
def test_sync_events_then_snapshot_markets_maps_series(pg, monkeypatch):
    monkeypatch.setattr(snapshot, "BATCH", 2)
    client = FakeClient([ev("E1", "S1"), ev("E2", "S2"), ev("E3", None), {"title": "no ticker"}, ev("E1", "S1")],
                        [{"ticker": "M1", "event_ticker": "E1"}, {"ticker": "M2", "event_ticker": "E2"},
                         {"ticker": "M3", "event_ticker": "E3"}, {"ticker": "M4", "event_ticker": "EX"},
                         {"ticker": "M5", "event_ticker": "E1"}, {"event_ticker": "E1"}])
    assert snapshot.sync_events(pg, client, NOW) == 4  # 3 + E1 again in its own page
    assert pg.info.transaction_status == 0  # committed per page, nothing left open
    n = snapshot.snapshot_markets(pg, client, NOW)
    pg.commit()
    got = dict(pg.execute("select ticker, series_ticker from market_snapshots").fetchall())
    assert n == 5 and got == {"M1": "S1", "M2": "S2", "M3": None, "M4": None, "M5": "S1"}
