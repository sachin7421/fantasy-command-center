"""Resolving a source id without asking the database every time.

Every sync loop calls `resolve` per row, and its first step is a SELECT on
player_id_map. Against the hosted database that is 20ms a row before any work
happens - thousands of rows a run, in several phases. The table is small
enough to hold, and it is OUR mapping, so holding it is cheap and safe.

What must not happen: a cache that goes stale within a run, so a mapping this
run wrote is not seen by the next lookup, or a manual override is ignored.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper


@pytest.fixture
def mapper(tmp_path):
    conn = db.init_db(tmp_path / "cache.db", force_sqlite=True)
    mapper = IdMapper(conn)
    mapper.upsert_players([
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF"},
        {"full_name": "Puka Nacua", "position": "WR", "team": "LAR"},
    ])
    conn.commit()
    yield mapper
    conn.close()


class _CountingConn:
    """Wraps a Database and counts player_id_map lookups."""

    def __init__(self, inner):
        self._inner = inner
        self.lookups = 0

    def execute(self, sql, params=None):
        if "FROM player_id_map" in sql and "SELECT" in sql:
            self.lookups += 1
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_repeated_source_id_is_not_looked_up_twice(mapper):
    counting = _CountingConn(mapper.conn)
    mapper.conn = counting

    first = mapper.resolve(source="sleeper", source_id="4984", name="Josh Allen",
                           position="QB", team="BUF")
    for _ in range(5):
        again = mapper.resolve(source="sleeper", source_id="4984", name="Josh Allen",
                               position="QB", team="BUF")
        assert again.player_key == first.player_key
    assert counting.lookups <= 1, f"{counting.lookups} lookups for one source id"


def test_a_mapping_written_this_run_is_visible_immediately(mapper):
    """The write path must seed the cache, or a run re-resolves every row it
    just learned - and the fuzzy branch is where the expensive mistakes are."""
    mapper.record_mapping("sleeper", "9493", "puka nacua|WR", "exact", 1.0)
    counting = _CountingConn(mapper.conn)
    mapper.conn = counting
    assert mapper.resolve(source="sleeper", source_id="9493").player_key == "puka nacua|WR"
    assert counting.lookups == 0


def test_a_bulk_mapping_is_visible_immediately_too(mapper):
    mapper.upsert_players([
        {"full_name": "Rookie Back", "position": "RB", "team": "NYJ", "sleeper_id": "12345"}
    ])
    counting = _CountingConn(mapper.conn)
    mapper.conn = counting
    assert mapper.resolve(source="sleeper", source_id="12345").player_key == "rookie back|RB"
    assert counting.lookups == 0


def test_the_cache_is_per_source(mapper):
    """Sleeper id 4984 and Yahoo id 4984 are different players."""
    mapper.record_mapping("sleeper", "4984", "josh allen|QB", "exact", 1.0)
    mapper.record_mapping("yahoo", "4984", "puka nacua|WR", "exact", 1.0)
    assert mapper.resolve(source="sleeper", source_id="4984").player_key == "josh allen|QB"
    assert mapper.resolve(source="yahoo", source_id="4984").player_key == "puka nacua|WR"


def test_a_manual_override_still_wins_over_a_cached_mapping(mapper):
    """Overrides are the escape hatch for a match this code got wrong."""
    mapper.record_mapping("sleeper", "4984", "puka nacua|WR", "exact", 1.0)
    assert mapper.resolve(source="sleeper", source_id="4984").player_key == "puka nacua|WR"

    mapper.overrides = {"sleeper": {"4984": "josh allen|QB"}}
    result = mapper.resolve(source="sleeper", source_id="4984")
    assert result.player_key == "josh allen|QB"
    assert result.method == "manual"


def test_a_mapping_stored_by_another_process_is_still_found(mapper):
    """The cache fills from the table on first use, not from nothing."""
    mapper.conn.execute(
        "INSERT INTO player_id_map(source, source_id, player_key, method, confidence, "
        "updated_at) VALUES (?,?,?,?,?,?)",
        ("espn", "777", "puka nacua|WR", "exact", 1.0, db.utcnow()),
    )
    mapper.conn.commit()
    assert mapper.resolve(source="espn", source_id="777").player_key == "puka nacua|WR"
