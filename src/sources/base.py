"""Common interface for external data adapters.

Every source is swappable (spec 3): each adapter subclasses `Source`, fetches
through `self.get_json`, and every fetch is cached in SQLite with a timestamp so
a dead source degrades to last-known-good instead of crashing a job.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any

import requests

from src import db
from src.storage import Database

log = logging.getLogger(__name__)

USER_AGENT = "fantasy-command-center/1.0 (personal league tool)"


class SourceUnavailable(RuntimeError):
    """Raised when a source failed and no usable cache exists."""


@dataclass
class FetchResult:
    payload: Any
    from_cache: bool
    fetched_at: str
    stale: bool = False

    @property
    def ok(self) -> bool:
        return self.payload is not None


class Source:
    """Base adapter: cached, retrying HTTP with graceful degradation."""

    name: str = "base"
    #: How long a cached copy stays usable when the live fetch fails.
    max_cache_age_hours: int = 36
    #: Politeness delay between consecutive requests.
    request_delay_seconds: float = 0.0

    def __init__(self, conn: Database, max_cache_age_hours: int | None = None):
        self.conn = conn
        if max_cache_age_hours is not None:
            self.max_cache_age_hours = max_cache_age_hours
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._last_request = 0.0

    # -- fetching ------------------------------------------------------------

    def get_json(
        self,
        url: str,
        cache_key: str | None = None,
        *,
        max_age_hours: float | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        retries: int = 3,
        force: bool = False,
    ) -> FetchResult:
        """GET JSON with cache-first-on-failure semantics.

        `max_age_hours` makes the cache authoritative for that long (used for
        the 5MB Sleeper player dump, which Sleeper asks be fetched at most daily).
        """
        cache_key = cache_key or f"{self.name}:{url}"

        if not force and max_age_hours is not None:
            cached = self._read_cache(cache_key, max_age_hours)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                self._throttle()
                response = self._session.get(
                    url, params=params, headers=headers, timeout=timeout
                )
                response.raise_for_status()
                payload = response.json()
                db.cache_put(self.conn, cache_key, self.name, payload)
                return FetchResult(payload, False, db.utcnow())
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)

        cached = self._read_cache(cache_key, max_age_hours=None)
        if cached is not None:
            age = _age_hours(cached.fetched_at)
            stale = age > self.max_cache_age_hours
            log.warning(
                "%s: fetch failed (%s); falling back to cache from %s (%.1fh old)%s",
                self.name, last_error, cached.fetched_at, age,
                " [STALE]" if stale else "",
            )
            return FetchResult(cached.payload, True, cached.fetched_at, stale=stale)

        raise SourceUnavailable(f"{self.name}: {url} failed and no cache exists: {last_error}")

    def _read_cache(self, cache_key: str, max_age_hours: float | None) -> FetchResult | None:
        cached = db.cache_get(self.conn, cache_key)
        if cached is None:
            return None
        payload, fetched_at = cached
        if max_age_hours is not None and _age_hours(fetched_at) > max_age_hours:
            return None
        return FetchResult(payload, True, fetched_at)

    def _throttle(self) -> None:
        if not self.request_delay_seconds:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.request_delay_seconds:
            time.sleep(self.request_delay_seconds - elapsed)
        self._last_request = time.monotonic()

    # -- interface -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Report whether this source is currently usable, for `fcc doctor`."""
        raise NotImplementedError


def _age_hours(iso_timestamp: str) -> float:
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then) / timedelta(hours=1)
