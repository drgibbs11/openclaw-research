"""The snapshot run still writes market_snapshots when events sync fails.

No DB and no network. Postgres coverage for the upsert itself stays in
tests/test_snapshot_events.py.

    python -m pytest tests/test_snapshot_continues.py -q
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "jobs")

import snapshot  # noqa: E402
from lib.kalshi import RateLimited  # noqa: E402

NOW = datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc)


class Conn:
    def __init__(self, calls):
        self.calls = calls

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")


def _patch_main(monkeypatch, *, events_exc=None, page_cursor="cur-99", markets_exc=None, markets_n=7):
    calls = []
    recorded = {}

    class StubClient:
        request_count = 4

        def __init__(self):
            self.page_cursor = page_cursor

    def sync_series(conn, client, now):
        calls.append("series")

    def sync_events(conn, client, now):
        calls.append("events")
        if events_exc is not None:
            raise events_exc
        return 3

    def snapshot_markets(conn, client, run_ts):
        calls.append("markets")
        if markets_exc is not None:
            raise markets_exc
        return markets_n

    def set_job_cursor(conn, name, cursor_ts=None, cursor_text=None, notes=None):
        calls.append("cursor")
        recorded["name"] = name
        recorded["cursor_ts"] = cursor_ts
        recorded["cursor_text"] = cursor_text
        recorded["notes"] = notes

    @contextmanager
    def connect():
        yield Conn(calls)

    monkeypatch.setattr(snapshot, "KalshiClient", StubClient)
    monkeypatch.setattr(snapshot, "sync_series", sync_series)
    monkeypatch.setattr(snapshot, "sync_events", sync_events)
    monkeypatch.setattr(snapshot, "snapshot_markets", snapshot_markets)
    monkeypatch.setattr(snapshot.db, "connect", connect)
    monkeypatch.setattr(snapshot.db, "set_job_cursor", set_job_cursor)
    return calls, recorded


@pytest.mark.parametrize("exc", [
    RateLimited("exhausted 12 retries on /events"),
    RuntimeError("events upsert blew up"),
])
def test_main_continues_to_snapshots_after_events_failure(monkeypatch, caplog, exc):
    exc.events_committed = {"seen": 10, "inserted": 1, "updated": 2, "unchanged": 7}
    calls, recorded = _patch_main(monkeypatch, events_exc=exc, page_cursor="cur-99")
    with caplog.at_level(logging.WARNING, logger="snapshot"):
        assert snapshot.main() == 0
    assert calls == ["series", "commit", "events", "rollback", "markets", "cursor"]
    assert "continuing to market snapshots with events already stored" in caplog.text
    assert type(exc).__name__ in caplog.text
    assert "resume cursor cur-99" in caplog.text
    assert "after 10 committed events" in caplog.text
    assert recorded["name"] == "snapshot"
    assert recorded["cursor_text"] == "cur-99"
    assert recorded["cursor_ts"] is not None
    assert recorded["notes"]["snapshots"] == 7
    assert recorded["notes"]["events_sync"] == type(exc).__name__
    assert recorded["notes"]["events_cursor"] == "cur-99"
    assert str(exc) in recorded["notes"]["events_error"]
    assert recorded["notes"]["events_committed"] == {
        "seen": 10, "inserted": 1, "updated": 2, "unchanged": 7,
    }


def test_main_happy_path_records_snapshots_only(monkeypatch, caplog):
    calls, recorded = _patch_main(monkeypatch, events_exc=None, page_cursor=None)
    with caplog.at_level(logging.WARNING, logger="snapshot"):
        assert snapshot.main() == 0
    assert calls == ["series", "commit", "events", "markets", "cursor"]
    assert recorded["notes"] == {"snapshots": 7}
    assert recorded["cursor_text"] is None
    assert "continuing to market snapshots" not in caplog.text


def test_main_snapshot_failure_still_raises(monkeypatch):
    calls, recorded = _patch_main(monkeypatch, markets_exc=RuntimeError("snapshot failed"))
    with pytest.raises(RuntimeError, match="snapshot failed"):
        snapshot.main()
    assert calls == ["series", "commit", "events", "markets"]
    assert "cursor" not in calls
    assert recorded == {}


def test_main_snapshot_failure_after_events_failure_still_raises(monkeypatch, caplog):
    calls, _recorded = _patch_main(
        monkeypatch,
        events_exc=RateLimited("exhausted 12 retries on /events"),
        markets_exc=RuntimeError("snapshot failed"),
    )
    with caplog.at_level(logging.WARNING, logger="snapshot"):
        with pytest.raises(RuntimeError, match="snapshot failed"):
            snapshot.main()
    assert calls == ["series", "commit", "events", "rollback", "markets"]
    assert "continuing to market snapshots" in caplog.text


def test_sync_events_commits_full_pages_then_reraises(monkeypatch):
    monkeypatch.setattr(snapshot, "BATCH", 2)
    written = []

    def write_events(conn, batch, totals):
        written.append([row["event_ticker"] for row in batch])
        totals["seen"] += len(batch)
        totals["inserted"] += len(batch)

    monkeypatch.setattr(snapshot, "write_events", write_events)

    class Client:
        page_cursor = "cur-after-page-1"

        def iter_events(self):
            yield {"event_ticker": "E1"}
            yield {"event_ticker": "E2"}
            yield {"event_ticker": "E3"}
            raise RateLimited("exhausted 12 retries on /events")

    with pytest.raises(RateLimited) as raised:
        snapshot.sync_events(object(), Client(), NOW)
    # E3 was still in the open batch when paging died, so it was not committed.
    assert written == [["E1", "E2"]]
    assert raised.value.events_committed == {
        "seen": 2, "inserted": 2, "updated": 0, "unchanged": 0,
    }


def test_events_failure_warning_includes_counts():
    exc = RateLimited("exhausted 12 retries on /events")
    exc.events_committed = {"seen": 1500, "inserted": 3, "updated": 4, "unchanged": 1493}
    text = snapshot._events_failure_warning(exc, "abc")
    assert "RateLimited: exhausted 12 retries on /events" in text
    assert "after 1500 committed events" in text
    assert "inserted 3" in text and "updated 4" in text and "unchanged 1493" in text
    assert "resume cursor abc" in text
    assert text.endswith("continuing to market snapshots with events already stored")
