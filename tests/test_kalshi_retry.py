"""Retry, Retry-After, and /events pacing. No network, no DB.

    python -m pytest tests/test_kalshi_retry.py -q
"""
from __future__ import annotations

import random
import sys
from email.utils import formatdate

import pytest
import requests

sys.path.insert(0, ".")

from lib import kalshi  # noqa: E402
from lib.kalshi import (  # noqa: E402
    BACKOFF_CAP_S,
    EVENTS_RECOVERY_DIVISOR,
    EVENTS_RECOVERY_STREAK,
    EVENTS_START_RPS,
    MAX_RETRIES,
    KalshiClient,
    RateLimited,
    backoff_seconds,
    parse_retry_after,
)


class Resp:
    def __init__(self, status, headers=None, payload=None):
        self.status_code = status
        self.headers = {} if headers is None else dict(headers)
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class BoundRng:
    """uniform() returns one end of the range so backoff tests are exact."""

    def __init__(self, end):
        self.end = end
        self.calls = []

    def uniform(self, lo, hi):
        self.calls.append((lo, hi))
        return lo if self.end == "lo" else hi


def scripted(responses):
    session = requests.Session()
    queue = list(responses)

    def _get(url, params=None, timeout=None):
        if not queue:
            raise AssertionError(f"unexpected GET {url}")
        return queue.pop(0)

    session.get = _get
    return session, queue


def install_clock(monkeypatch):
    """Record backoff sleeps. Throttle waits see a clock that never needs them."""
    sleeps = []
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 10_000
        return clock["t"]

    monkeypatch.setattr(kalshi.time, "monotonic", monotonic)
    monkeypatch.setattr(kalshi.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def client_for(monkeypatch, responses, rng=None):
    install_clock(monkeypatch)
    session, queue = scripted(responses)
    client = KalshiClient(throttle_rps=0, session=session)
    if rng is not None:
        client._rng = rng
    return client, queue


# ---------------------------------------------------------------- parse / backoff

def test_parse_retry_after_seconds_date_and_garbage():
    now = 1_800_000_000.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("nan") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("-4") == 0.0
    assert parse_retry_after(formatdate(now + 15, usegmt=True), now=now) == pytest.approx(15.0)
    assert parse_retry_after(formatdate(now - 10, usegmt=True), now=now) == 0.0


def test_backoff_honors_retry_after_and_caps_it():
    rng = BoundRng("hi")
    assert backoff_seconds(32, 5, rng) == 5
    assert backoff_seconds(32, 0, rng) == 0
    assert backoff_seconds(32, 120, rng) == BACKOFF_CAP_S == 60
    assert rng.calls == []  # a header is not jittered down


def test_backoff_jitter_is_half_to_full_of_the_capped_delay():
    high = BoundRng("hi")
    low = BoundRng("lo")
    assert backoff_seconds(1, None, high) == 1
    assert backoff_seconds(8, None, low) == 4
    assert backoff_seconds(128, None, high) == 60
    assert backoff_seconds(128, None, low) == 30
    assert high.calls == [(0.5, 1.0), (30.0, 60.0)]
    assert low.calls == [(4.0, 8.0), (30.0, 60.0)]


# ---------------------------------------------------------------- get()

def test_retry_after_seconds_override_exponential_delay(monkeypatch):
    sleeps = install_clock(monkeypatch)
    session, queue = scripted([
        Resp(429, {"Retry-After": "4"}),
        Resp(429, {"Retry-After": "4"}),
        Resp(429, {"Retry-After": "4"}),
        Resp(200, payload={"ok": True}),
    ])
    client = KalshiClient(throttle_rps=0, session=session)
    client._rng = BoundRng("hi")
    assert client.get("/markets") == {"ok": True}
    assert sleeps == [4.0, 4.0, 4.0]
    assert queue == []
    assert client._rng.calls == []


def test_retry_after_is_capped_at_60(monkeypatch):
    sleeps = install_clock(monkeypatch)
    session, _queue = scripted([
        Resp(429, {"Retry-After": "120"}),
        Resp(200, payload={"ok": True}),
    ])
    client = KalshiClient(throttle_rps=0, session=session)
    assert client.get("/markets") == {"ok": True}
    assert sleeps == [60.0]


def test_http_date_retry_after_is_honored(monkeypatch):
    sleeps = install_clock(monkeypatch)
    now = 1_800_000_000
    monkeypatch.setattr(kalshi.time, "time", lambda: float(now))
    session, _queue = scripted([
        Resp(429, {"Retry-After": formatdate(now + 15, usegmt=True)}),
        Resp(200, payload={"ok": True}),
    ])
    client = KalshiClient(throttle_rps=0, session=session)
    assert client.get("/events")["ok"] is True
    assert sleeps == [pytest.approx(15.0)]


def test_unparseable_retry_after_falls_back_to_backoff(monkeypatch):
    sleeps = install_clock(monkeypatch)
    session, _queue = scripted([
        Resp(429, {"Retry-After": "soon"}),
        Resp(429, {"Retry-After": ""}),
        Resp(200, payload={"ok": True}),
    ])
    client = KalshiClient(throttle_rps=0, session=session)
    client._rng = BoundRng("hi")
    assert client.get("/markets") == {"ok": True}
    # No header to honor: full jittered delay, then double it. Both capped later.
    assert sleeps == [1.0, 2.0]


def test_exponential_backoff_is_capped_and_jittered(monkeypatch):
    sleeps = install_clock(monkeypatch)
    session, queue = scripted([Resp(429) for _ in range(MAX_RETRIES)])
    client = KalshiClient(throttle_rps=0, session=session)
    client._rng = random.Random(0)
    with pytest.raises(RateLimited, match=r"exhausted 12 retries on /markets"):
        client.get("/markets")
    assert MAX_RETRIES == 12
    assert len(sleeps) == MAX_RETRIES - 1  # no sleep after the attempt that gives up
    assert queue == []
    assert client.request_count == MAX_RETRIES
    assert 0.5 <= sleeps[0] <= 1.0
    assert all(0 < s <= BACKOFF_CAP_S for s in sleeps)
    # delay series hits the 60s cap on the 7th wait (1,2,4,8,16,32,60...)
    assert all(30 <= s <= BACKOFF_CAP_S for s in sleeps[6:])


def test_high_jitter_backoff_series_matches_the_cap(monkeypatch):
    sleeps = install_clock(monkeypatch)
    session, _queue = scripted([Resp(429) for _ in range(MAX_RETRIES)])
    client = KalshiClient(throttle_rps=0, session=session)
    client._rng = BoundRng("hi")
    with pytest.raises(RateLimited):
        client.get("/markets")
    assert sleeps == [1, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60]


def test_429_on_events_still_honors_retry_after(monkeypatch):
    """The header path is the same for /events; pacing sleeps are not mixed in."""
    sleeps = install_clock(monkeypatch)
    session, _queue = scripted([
        Resp(429, {"Retry-After": "3"}),
        Resp(200, payload={"events": []}),
    ])
    client = KalshiClient(throttle_rps=5, session=session)
    assert client.get("/events") == {"events": []}
    assert sleeps == [3.0]


def test_transport_error_retries_then_succeeds(monkeypatch):
    sleeps = install_clock(monkeypatch)
    session, _queue = scripted([
        requests.ConnectionError("reset"),
        Resp(200, payload={"ok": True}),
    ])
    # scripted returns responses; ConnectionError must be raised by get().
    calls = {"n": 0}
    responses = [Resp(200, payload={"ok": True})]

    def _get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("reset")
        return responses.pop(0)

    session.get = _get
    client = KalshiClient(throttle_rps=0, session=session)
    client._rng = BoundRng("hi")
    assert client.get("/markets") == {"ok": True}
    assert sleeps == [1.0]
    assert client.request_count == 1  # the failed socket call is not a response


def test_terminal_500_is_not_reported_as_rate_limited(monkeypatch):
    install_clock(monkeypatch)
    session, _queue = scripted([Resp(503) for _ in range(MAX_RETRIES)])
    client = KalshiClient(throttle_rps=0, session=session)
    client._rng = BoundRng("lo")
    with pytest.raises(requests.HTTPError):
        client.get("/markets")


def test_public_client_sends_no_credentials():
    client = KalshiClient(throttle_rps=5)
    names = {k.lower() for k in client.session.headers}
    assert "authorization" not in names
    assert not any(n.startswith("kalshi-access") for n in names)


# ---------------------------------------------------------------- /events pace

def test_events_paging_starts_at_half_request_per_second():
    assert EVENTS_START_RPS == 0.5
    client = KalshiClient(throttle_rps=5)
    assert client._interval("/events") == pytest.approx(2.0)
    assert client._interval("/markets") == pytest.approx(0.2)
    assert client._interval("/series") == pytest.approx(0.2)


def test_events_stay_at_configured_rate_when_that_is_already_slower():
    client = KalshiClient(throttle_rps=0.25)
    assert client._interval("/events") == pytest.approx(4.0)


def test_events_get_waits_out_the_half_rps_interval(monkeypatch):
    sleeps = []
    monkeypatch.setattr(kalshi.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(kalshi.time, "monotonic", lambda: 50.0)
    session, _queue = scripted([Resp(200, payload={"events": []})])
    client = KalshiClient(throttle_rps=5, session=session)
    client._last_call = 50.0
    client.get("/events")
    assert sleeps == [pytest.approx(2.0)]


def test_markets_keep_the_configured_rate(monkeypatch):
    sleeps = []
    monkeypatch.setattr(kalshi.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(kalshi.time, "monotonic", lambda: 50.0)
    session, _queue = scripted([Resp(200, payload={"markets": []})])
    client = KalshiClient(throttle_rps=5, session=session)
    client._last_call = 50.0
    client.get("/markets")
    assert sleeps == [pytest.approx(0.2)]


def test_events_ramp_up_slower_than_other_paths():
    assert EVENTS_RECOVERY_STREAK == 80
    assert EVENTS_RECOVERY_DIVISOR == 1.2
    client = KalshiClient(throttle_rps=5)
    for _ in range(EVENTS_RECOVERY_STREAK - 1):
        client._on_success("/events")
    assert client._interval("/events") == pytest.approx(2.0)
    client._on_success("/events")
    assert client._interval("/events") == pytest.approx(2.0 / EVENTS_RECOVERY_DIVISOR)

    # A 429 on /events does not drag /markets down with it.
    assert client._interval("/markets") == pytest.approx(0.2)
    client._on_throttled("/events")
    assert client._interval("/events") == pytest.approx(2.0)  # already at the 0.5 floor
    assert client._interval("/markets") == pytest.approx(0.2)

    client._on_throttled("/markets")
    assert client._interval("/markets") == pytest.approx(0.4)
    for _ in range(19):
        client._on_success("/markets")
    assert client._interval("/markets") == pytest.approx(0.4)
    client._on_success("/markets")
    assert client._interval("/markets") == pytest.approx(0.4 / 1.5)


def test_paginate_keeps_the_cursor_of_the_page_that_failed(monkeypatch):
    client = KalshiClient(throttle_rps=0)
    calls = {"n": 0}

    def get(path, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"events": [{"event_ticker": "E1"}], "cursor": "c1"}
        raise RateLimited("exhausted 12 retries on /events")

    monkeypatch.setattr(client, "get", get)
    with pytest.raises(RateLimited):
        list(client.paginate("/events", "events"))
    assert client.page_cursor == "c1"
