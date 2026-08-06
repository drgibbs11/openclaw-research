"""Kalshi public market-data client.

G2: read-only. Public GETs only, no auth, no signing, no order endpoints.
G6: polite. Token-bucket throttle + Retry-After-honoring backoff (D17).

All numeric wire values are fixed-point decimal *strings* (D1/D2/D3); use the
`dec` / `cents` helpers rather than int() anywhere.
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"  # D15
MAX_LIMIT = 1000
# /events rejects limit > 200 with a 400, even though the OpenAPI spec documents
# "Maximum value is 1000" for it like every other paginated endpoint (D21).
EVENTS_MAX_LIMIT = 200
MAX_RETRIES = 6


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
        # Adaptive: a 429 widens the interval for *subsequent* requests, not
        # just the one that failed. Per-request backoff alone is not enough —
        # it retries, succeeds, then immediately resumes the same rate and
        # drains the bucket again. Observed as a 179-request 429 storm against
        # /events at 8 rps. G6: when in doubt, slower.
        self._min_interval = self._base_interval
        self._ok_streak = 0
        self._last_call = 0.0
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-screener/1.0"})
        self.request_count = 0

    # ------------------------------------------------------------- transport
    def _wait(self) -> None:
        if self._min_interval:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _on_throttled(self) -> None:
        """Halve the sustained request rate, to a floor of 0.5 req/s."""
        self._ok_streak = 0
        widened = max(self._base_interval, self._min_interval) * 2 or 0.25
        self._min_interval = min(widened, 2.0)
        log.warning("throttling back to %.2f req/s", 1.0 / self._min_interval)

    def _on_success(self) -> None:
        """Recover toward the configured rate after a clean run."""
        if self._min_interval <= self._base_interval:
            return
        self._ok_streak += 1
        if self._ok_streak >= 20:
            self._ok_streak = 0
            self._min_interval = max(self._base_interval, self._min_interval / 1.5)
            log.info("recovering to %.2f req/s", 1.0 / self._min_interval)

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with throttle and exponential backoff. Raises on terminal failure."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        # Python bools stringify to "True"/"False"; send the JSON spelling.
        params = {k: ("true" if v is True else "false" if v is False else v)
                  for k, v in (params or {}).items() if v is not None}
        delay = 1.0

        for attempt in range(MAX_RETRIES):
            self._wait()
            try:
                resp = self.session.get(url, params=params, timeout=45)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                log.warning("%s on %s, retrying in %.1fs", type(exc).__name__, path, delay)
                time.sleep(delay)
                delay *= 2
                continue

            self.request_count += 1

            if resp.status_code == 429:
                # G6: honor Retry-After when present.
                self._on_throttled()
                wait = float(resp.headers.get("Retry-After") or delay)
                log.warning("429 on %s, sleeping %.1fs", path, wait)
                time.sleep(wait)
                delay *= 2
                continue

            if resp.status_code >= 500:
                if attempt == MAX_RETRIES - 1:
                    resp.raise_for_status()
                log.warning("%s on %s, retrying in %.1fs", resp.status_code, path, delay)
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 401:
                # G2: stop, do not implement signing.
                raise PermissionError(
                    f"401 on public endpoint {path} — G2 says stop and record it, "
                    "not implement request signing."
                )

            resp.raise_for_status()
            self._on_success()
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
