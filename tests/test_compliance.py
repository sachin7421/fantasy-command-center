"""The Yahoo API agreement, enforced in code rather than in intention.

Signed 2026-09-10. Five obligations, and four of them are the kind that decay
quietly: somebody adds a caching call, a weight gets tuned back up, a footer is
restyled and the attribution goes with it. So each one is a test.

The boundary being enforced - what counts as "Yahoo-derived":

  Yahoo's, must never touch disk
      API responses: settings, teams, rosters, free agents, transactions,
      draft results, budgets. Yahoo player keys. Anything fetched from the API.

  Ours, may persist
      Projections from Sleeper, ESPN, FantasyPros, nflverse. Our blends, VORP,
      tiers and recommendations. Draft picks WE typed in. Learned FAAB
      coefficients - a shrunk dollars-per-point number from which no Yahoo fact
      is recoverable.

  The user's own, may persist
      League scoring rules and roster slots. Transcribed by hand from the
      league settings page, never fetched from the API, so they are this
      manager's configuration of his own league and live in config.yaml.
"""
from __future__ import annotations

import pytest

from src import db


def test_the_cache_refuses_yahoo(tmp_path):
    """The one structural guard everything else rests on.

    Every Yahoo fetch in the client funnels through `_cached`, which calls
    `cache_put`. Refusing at that single point makes persistence impossible
    rather than merely discouraged - a future caller who has never read the
    agreement still cannot write Yahoo data to disk.
    """
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    try:
        with pytest.raises(ValueError, match="Yahoo"):
            db.cache_put(conn, "yahoo:settings:nfl.l.796511", "yahoo", {"a": 1})
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0
    finally:
        conn.close()


@pytest.mark.parametrize("source", ["yahoo", "YAHOO", "Yahoo", "yahoo_fantasy"])
def test_the_refusal_is_not_case_or_suffix_sensitive(tmp_path, source):
    """A guard that a different spelling walks past is not a guard."""
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    try:
        with pytest.raises(ValueError):
            db.cache_put(conn, f"{source}:x", source, {"a": 1})
    finally:
        conn.close()


def test_other_sources_still_cache_normally(tmp_path):
    """The guard must not break the sources the agreement does not cover."""
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    try:
        for source in ("sleeper", "espn", "fantasypros", "nflverse"):
            db.cache_put(conn, f"{source}:players", source, {"ok": True})
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 4
    finally:
        conn.close()


def test_yahoo_projections_ship_disabled_and_weighted_zero():
    """Obligation 3: optional, default weight zero.

    A weight is one number in a config file and is exactly the kind of thing
    that gets nudged during tuning without anyone remembering it is a contract
    term.
    """
    from src.config import Config

    cfg = Config.load("config.yaml")
    weight = cfg.get("projections.weights.yahoo")
    assert weight in (0, 0.0), f"Yahoo projection weight ships at {weight}, must be 0"
    assert cfg.get("sources.yahoo.enabled") is False, (
        "the Yahoo projection source must ship disabled"
    )


# --- obligation 1: Yahoo responses live in memory for one run ----------------

class _FakeQuery:
    """Stands in for yfpy so these run with no OAuth and no network."""

    def __init__(self, payload=None, fail_after: int | None = None):
        self.payload = payload if payload is not None else {"name": b"Extra Fun League"}
        self.calls = 0
        self.fail_after = fail_after

    def get_league_settings(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("Yahoo is unreachable")
        return self.payload


def _client(tmp_path, query):
    from src.config import Config
    from src.yahoo_client import YahooClient

    cfg = Config.load("config.yaml")
    conn = db.init_db(tmp_path / "y.db", force_sqlite=True)
    client = YahooClient(cfg, conn)
    client._query = query
    client._league_key = "nfl.l.796511"
    return client, conn


def test_a_yahoo_response_never_reaches_disk(tmp_path):
    """The whole obligation, in one assertion."""
    query = _FakeQuery()
    client, conn = _client(tmp_path, query)
    try:
        payload, _ = client._cached("yahoo:settings:x", query.get_league_settings)
        assert payload["name"] == "Extra Fun League"
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0, (
            "a Yahoo response was written to source_cache"
        )
    finally:
        conn.close()


def test_a_response_is_reused_within_the_run(tmp_path):
    """Held in memory for the duration of a run, so one fetch serves the run.

    This is also the cost control: with disk caching forbidden, an unmemoised
    client would re-fetch on every caller, multiplying API calls by however
    many places happen to ask.
    """
    query = _FakeQuery()
    client, conn = _client(tmp_path, query)
    try:
        client._cached("yahoo:settings:x", query.get_league_settings)
        client._cached("yahoo:settings:x", query.get_league_settings)
        client._cached("yahoo:settings:x", query.get_league_settings)
        assert query.calls == 1, f"hit Yahoo {query.calls} times for one key"
    finally:
        conn.close()


def test_the_memory_does_not_outlive_the_run(tmp_path):
    """A second client is a second run, and starts with nothing."""
    query = _FakeQuery()
    client, conn = _client(tmp_path, query)
    try:
        client._cached("yahoo:settings:x", query.get_league_settings)
        second, conn2 = _client(tmp_path, query)
        second._cached("yahoo:settings:x", query.get_league_settings)
        conn2.close()
        assert query.calls == 2, "a run reused another run's Yahoo data"
    finally:
        conn.close()


def test_when_yahoo_is_down_the_job_says_so_instead_of_using_stale_data(tmp_path):
    """The deliberate reversal of this project's own standard 4.

    Everywhere else, an external source that fails falls back to cached data
    with a warning. For Yahoo that fallback is now forbidden - there is no
    stored copy to fall back TO - so a job must fail loudly rather than quietly
    act on last week's roster. A stale roster produces confident, wrong advice,
    which is worse than no advice.
    """
    query = _FakeQuery(fail_after=0)
    client, conn = _client(tmp_path, query)
    try:
        with pytest.raises(ConnectionError):
            client._cached("yahoo:settings:x", query.get_league_settings)
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0
    finally:
        conn.close()
