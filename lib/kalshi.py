"""Kalshi public market-data client.

G2: read-only. Public GETs only, no auth, no signing, no order endpoints.
G6: polite. Token-bucket throttle + Retry-After-honoring backoff (D17).

All numeric wire values are fixed-point decimal *strings* (D1/D2/D3); use the
`dec` / `cents` helpers rather than int() anywhere.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"  # D15
MAX_LIMIT = 1000
# /events rejects limit > 200 with a 400, even though the OpenAPI spec documents
# "Maximum value is 1000" for it like every other paginated endpoint (D21).
EVENTS_MAX_LIMIT = 200
# A 429 storm on /events used to exhaust 6 tries (about 1s, 2s, 4s, 8s, 16s,
# 32s — ~63s total) and abort the snapshot before any market_snapshots write.
# Twelve tries with the cap below is several minutes of patience instead.
MAX_RETRIES = 12
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 60.0
# Floor after a 429. /events paging also starts here (see _interval): that
# list is limited harder than /markets or /series (D24). Climbing back with
# the old step (20 successes, rate ×1.5) bounced 0.50 ↔ 0.75 req/s and
# retripped the limit. /events now needs a longer clean streak and a smaller step.
MIN_RPS = 0.5
EVENTS_START_RPS = 0.5
EVENTS_RECOVERY_STREAK = 80
EVENTS_RECOVERY_DIVISOR = 1.2
RECOVERY_STREAK = 20
RECOVERY_DIVISOR = 1.5


# --------------------------------------------------------------- conversions

def dec(value: Any) -> Decimal | None:
    """Parse a FixedPointDollars/FixedPointCount string to Decimal.

    Returns None for absent/empty values so callers can distinguish "no quote"
    from "zero".
    """
    if value is None or value == "":
        return None
    return Decimal(str(value))


def cents(value: Any) -> Decimal | None:
    """Dollars string -> cents. '0.0100' -> 1.00. Prices may be sub-cent (D3)."""
    d = dec(value)
    return None if d is None else d * 100


class RateLimited(Exception):
    pass


def parse_retry_after(value: Any, now: float | None = None) -> float | None:
    """Retry-After as a delay in seconds, or None if it isn't one.

    Delta-seconds (``"7"``, ``"1.5"``) and an HTTP-date both count. A date is
    seconds from ``now`` (default ``time.time()``). Missing, blank, and
    unparseable values return None so the caller uses exponential backoff
    instead of raising on a header it doesn't understand.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        if seconds != seconds or seconds in (float("inf"), float("-inf")):
            return None
        return max(0.0, seconds)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else now
    return max(0.0, when.timestamp() - current)


def backoff_seconds(delay: float, retry_after: float | None, rng: random.Random) -> float:
    """Seconds to wait before the next attempt.

    A parsed Retry-After is the wait, capped at ``BACKOFF_CAP_S``, with nothing
    subtracted: don't retry earlier than the server asked, and don't sleep
    longer than the cap. Without a header, ``delay`` (the exponential series)
    is capped the same way and then jittered uniformly into ``[half, full]``
    so overlapping runs don't retry on the same instant.
    """
    if retry_after is not None:
        return min(BACKOFF_CAP_S, max(0.0, retry_after))
    capped = min(BACKOFF_CAP_S, max(0.0, delay))
    if capped == 0:
        return 0.0
    return rng.uniform(capped * 0.5, capped)


class KalshiClient:
    def __init__(
        self,
        base_url: str | None = None,
        throttle_rps: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("KALSHI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        rps = throttle_rps if throttle_rps is not None else float(os.environ.get("THROTTLE_RPS", "5"))
        self._base_interval = 1.0 / rps if rps > 0 else 0.0
        # Per-path sustained interval. A 429 widens the interval for *later*
        # requests on that path, not just the one that failed. Per-request
        # backoff alone retries, succeeds, then immediately resumes the same
        # rate and drains the bucket again (D24). /events starts at
        # EVENTS_START_RPS inside _interval(); other paths start at the
        # configured rate. G6: when in doubt, slower.
        self._intervals: dict[str, float] = {}
        self._streaks: dict[str, int] = {}
        self._last_call = 0.0
        self._rng = random.Random()
        # Cursor of the page currently being fetched (None on the first page).
        # Left set when get() raises, so the caller can record where paging stopped.
        self.page_cursor: str | None = None
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-screener/1.0"})
        self.request_count = 0

    # ------------------------------------------------------------- transport
    def _pace_key(self, path: str) -> str:
        return "/" + path.split("?", 1)[0].strip("/")

    def _interval(self, path: str) -> float:
        key = self._pace_key(path)
        if key not in self._intervals:
            if key == "/events":
                # Slower of the configured throttle and EVENTS_START_RPS.
                # A THROTTLE_RPS below 0.5 stays slower; the default of 5 does not.
                self._intervals[key] = max(self._base_interval, 1.0 / EVENTS_START_RPS)
                log.info("pacing /events from %.2f req/s", 1.0 / self._intervals[key])
            else:
                self._intervals[key] = self._base_interval
        return self._intervals[key]

    def _recovery_policy(self, key: str) -> tuple[int, float]:
        if key == "/events":
            return EVENTS_RECOVERY_STREAK, EVENTS_RECOVERY_DIVISOR
        return RECOVERY_STREAK, RECOVERY_DIVISOR

    def _wait(self, path: str) -> None:
        interval = self._interval(path)
        if interval:
            elapsed = time.monotonic() - self._last_call
            if elapsed < interval:
                time.sleep(interval - elapsed)
        self._last_call = time.monotonic()

    def _on_throttled(self, path: str) -> None:
        """Halve this path's sustained rate, down to a floor of MIN_RPS."""
        key = self._pace_key(path)
        self._streaks[key] = 0
        current = self._interval(path)
        widened = max(self._base_interval, current) * 2 or 0.25
        self._intervals[key] = min(widened, 1.0 / MIN_RPS)
        log.warning("throttling back to %.2f req/s", 1.0 / self._intervals[key])

    def _on_success(self, path: str) -> None:
        """Recover this path toward the configured rate after a clean stretch.

        /events takes a longer streak and a smaller step than other paths, so
        a brief clear spell doesn't jump 0.50 → 0.75 req/s and draw another 429.
        """
        key = self._pace_key(path)
        current = self._interval(path)
        if current <= self._base_interval:
            return
        streak = self._streaks.get(key, 0) + 1
        need, divisor = self._recovery_policy(key)
        if streak < need:
            self._streaks[key] = streak
            return
        self._streaks[key] = 0
        recovered = max(self._base_interval, current / divisor)
        self._intervals[key] = recovered
        if recovered > 0:
            log.info("recovering to %.2f req/s", 1.0 / recovered)
        else:
            log.info("recovering to unlimited req/s")

    def _backoff_and_sleep(self, delay: float, retry_after: float | None, reason: str) -> float:
        wait = backoff_seconds(delay, retry_after, self._rng)
        if retry_after is not None:
            log.warning("%s, Retry-After %.1fs, sleeping %.1fs", reason, retry_after, wait)
        else:
            log.warning("%s, sleeping %.1fs", reason, wait)
        time.sleep(wait)
        return min(max(delay, 0.0) * 2, BACKOFF_CAP_S)

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with throttle and exponential backoff. Raises on terminal failure."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        # Python bools stringify to "True"/"False"; send the JSON spelling.
        params = {k: ("true" if v is True else "false" if v is False else v)
                  for k, v in (params or {}).items() if v is not None}
        delay = BACKOFF_INITIAL_S

        for attempt in range(MAX_RETRIES):
            self._wait(path)
            try:
                resp = self.session.get(url, params=params, timeout=45)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                delay = self._backoff_and_sleep(delay, None, f"{type(exc).__name__} on {path}")
                continue

            self.request_count += 1

            if resp.status_code == 429:
                # G6: honor Retry-After when it parses; otherwise jittered backoff.
                self._on_throttled(path)
                if attempt == MAX_RETRIES - 1:
                    break
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                delay = self._backoff_and_sleep(delay, retry_after, f"429 on {path}")
                continue

            if resp.status_code >= 500:
                if attempt == MAX_RETRIES - 1:
                    resp.raise_for_status()
                delay = self._backoff_and_sleep(delay, None, f"{resp.status_code} on {path}")
                continue

            if resp.status_code == 401:
                # G2: stop, do not implement signing.
                raise PermissionError(
                    f"401 on public endpoint {path} — G2 says stop and record it, "
                    "not implement request signing."
                )

            resp.raise_for_status()
            self._on_success(path)
            return resp.json()

        raise RateLimited(f"exhausted {MAX_RETRIES} retries on {path}")

    # ------------------------------------------------------------ pagination
    def paginate(
        self,
        path: str,
        key: str,
        params: dict | None = None,
        max_pages: int | None = None,
    ) -> Iterator[tuple[list[dict], int]]:
        """Yield (page_items, page_index) until the cursor is absent or empty.

        Stops on cursor, never on short pages (§11). A repeated cursor also
        terminates — defensive against a server that echoes it forever.
        """
        params = dict(params or {})
        cursor = None
        seen_cursors: set[str] = set()
        page = 0

        while True:
            if cursor:
                params["cursor"] = cursor
            self.page_cursor = cursor
            body = self.get(path, params)
            items = body.get(key) or []
            yield items, page
            page += 1

            cursor = body.get("cursor")
            if not cursor:
                return
            if cursor in seen_cursors:
                log.warning("repeated cursor on %s, stopping at page %d", path, page)
                return
            seen_cursors.add(cursor)
            if max_pages is not None and page >= max_pages:
                return

    def iter_items(self, path: str, key: str, params: dict | None = None,
                   max_pages: int | None = None) -> Iterator[dict]:
        for items, _ in self.paginate(path, key, params, max_pages):
            yield from items

    # -------------------------------------------------------------- surfaces
    def list_series(self, **params) -> list[dict]:
        """GET /series — returns ALL series unpaginated (~12.5k, ~16MB). D5."""
        return self.get("/series", params).get("series") or []

    def get_series(self, series_ticker: str) -> dict | None:
        return self.get(f"/series/{series_ticker}").get("series")

    def iter_events(self, with_nested_markets: bool = False,
                    max_pages: int | None = None, **params) -> Iterator[dict]:
        # max_pages must be an explicit parameter: left to **params it would be
        # sent to the API as a query string field while pagination ran unbounded.
        params = {"limit": EVENTS_MAX_LIMIT,  # D21
                  "with_nested_markets": with_nested_markets, **params}
        return self.iter_items("/events", "events", params, max_pages)

    def iter_markets(self, status: str | None = None, exclude_mve: bool = True,
                     max_pages: int | None = None, **params) -> Iterator[dict]:
        """D8: exclude multivariate combo markets by default."""
        params = {"limit": MAX_LIMIT, "status": status, **params}
        if exclude_mve:
            params["mve_filter"] = "exclude"
        return self.iter_items("/markets", "markets", params, max_pages)

    def iter_historical_markets(self, series_ticker: str,
                                max_pages: int | None = None, **params) -> Iterator[dict]:
        """Deep archive for one series (D26).

        `/markets` only exposes a rolling recent window; this endpoint reaches
        back years. It has no time or status filter — `min_settled_ts` and
        friends are accepted and silently ignored — but results come back
        newest-first, so a caller wanting a bounded window can stop early
        rather than draining the whole series.
        The spec documents `mve_filter` on this endpoint, but sending it —
        with its only documented value, `exclude` — returns a 400 (D27). Combo
        markets are filtered client-side instead.
        """
        params = {"limit": MAX_LIMIT, "series_ticker": series_ticker, **params}
        for m in self.iter_items("/historical/markets", "markets", params, max_pages):
            if not m.get("mve_collection_ticker"):
                yield m

    def iter_trades(self, ticker: str, historical: bool = False,
                    max_pages: int | None = None, **params) -> Iterator[tuple[dict, int]]:
        """Public tape. D11: block trades excluded — they are negotiated
        off-book and do not reflect a maker quoting the screen.

        Yields (trade, page_index) so callers can enforce a page cap and know
        whether they truncated.
        """
        path = "/historical/trades" if historical else "/markets/trades"
        params = {"limit": MAX_LIMIT, "ticker": ticker, "is_block_trade": "false", **params}
        for items, page in self.paginate(path, "trades", params, max_pages):
            for t in items:
                yield t, page
