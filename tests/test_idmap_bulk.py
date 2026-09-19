"""Registering thousands of players without a round trip each.

`sync_players` walks ~3,300 players and spends six to eight statements on each
one: a lookup by yahoo_id, a lookup by player_key, an insert or update, and a
write per source id. At the 20ms round trip measured against the hosted
database on 19 Sep that is minutes of pure latency, and it is the first of
several phases shaped the same way - which is how `fcc sync` came to overrun a
20-minute job timeout and cancel two morning runs.

The player key is computed in Python, so nothing in the loop actually needs an
answer from the database before the next row. These are the same guarantees
the one-at-a-time path makes, asserted against the bulk one.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper


@pytest.fixture
def mapper(tmp_path):
    conn = db.init_db(tmp_path / "bulk.db", force_sqlite=True)
    yield IdMapper(conn)
    conn.close()


def _player(mapper, key):
    return mapper.conn.fetchone("SELECT * FROM players WHERE player_key=?", (key,))


def test_many_players_are_registered_and_their_keys_returned_in_order(mapper):
    keys = mapper.upsert_players([
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "sleeper_id": "4984"},
        {"full_name": "Puka Nacua", "position": "WR", "team": "LAR", "sleeper_id": "9493"},
    ])
    assert keys == ["josh allen|QB", "puka nacua|WR"]
    assert _player(mapper, "josh allen|QB")["full_name"] == "Josh Allen"
    assert _player(mapper, "puka nacua|WR")["team"] == "LAR"


def test_bulk_upsert_merges_ids_without_erasing(mapper):
    """The invariant from the single-row path: a partial update from one
    source never erases another source's id."""
    mapper.upsert_players([
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "yahoo_id": "30977"}
    ])
    mapper.upsert_players([
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "sleeper_id": "4984"}
    ])
    row = _player(mapper, "josh allen|QB")
    assert row["yahoo_id"] == "30977"
    assert row["sleeper_id"] == "4984"


def test_a_later_pass_updates_what_it_knows_and_leaves_the_rest(mapper):
    mapper.upsert_players([
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "bye_week": 7,
         "status": "ACT"}
    ])
    mapper.upsert_players([{"full_name": "Josh Allen", "position": "QB", "team": "NYJ"}])
    row = _player(mapper, "josh allen|QB")
    assert row["team"] == "NYJ"
    assert row["bye_week"] == 7
    assert row["status"] == "ACT"


def test_every_source_id_becomes_a_mapping(mapper):
    mapper.upsert_players([{
        "full_name": "Josh Allen", "position": "QB", "team": "BUF",
        "yahoo_id": "30977", "sleeper_id": "4984", "gsis_id": "00-0034857",
        "espn_id": "3918298",
    }])
    rows = {
        (r["source"], r["source_id"]): r["player_key"]
        for r in mapper.conn.fetchall("SELECT source, source_id, player_key FROM player_id_map")
    }
    assert rows == {
        ("yahoo", "30977"): "josh allen|QB",
        ("sleeper", "4984"): "josh allen|QB",
        ("nflverse", "00-0034857"): "josh allen|QB",
        ("espn", "3918298"): "josh allen|QB",
    }


def test_a_player_with_no_source_ids_is_still_registered(mapper):
    assert mapper.upsert_players([{"full_name": "Practice Squad Guy", "position": "RB"}])
    assert _player(mapper, "practice squad guy|RB") is not None


def test_no_players_is_a_no_op(mapper):
    assert mapper.upsert_players([]) == []
    assert mapper.conn.scalar("SELECT COUNT(*) FROM players") == 0


def test_the_same_player_twice_in_one_batch_does_not_collide(mapper):
    """Sleeper lists a player once, but two pages pasted or two sources merged
    in one call must not make the statement fail on its own conflict."""
    keys = mapper.upsert_players([
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "yahoo_id": "30977"},
        {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "sleeper_id": "4984"},
    ])
    assert keys == ["josh allen|QB", "josh allen|QB"]
    row = _player(mapper, "josh allen|QB")
    assert row["yahoo_id"] == "30977" and row["sleeper_id"] == "4984"


def test_bulk_and_single_agree_on_the_stored_row(mapper, tmp_path):
    """The two paths must not drift: same input, same row."""
    record = {
        "full_name": "Amon-Ra St. Brown", "position": "WR", "team": "DET",
        "bye_week": 5, "status": "ACT", "first_name": "Amon-Ra",
        "last_name": "St. Brown", "sleeper_id": "7525", "yahoo_id": "33413",
    }
    mapper.upsert_players([record])
    bulk = dict(_player(mapper, "amon ra st brown|WR"))

    other_conn = db.init_db(tmp_path / "single.db", force_sqlite=True)
    other = IdMapper(other_conn)
    other.upsert_player(**record)
    single = dict(other.conn.fetchone(
        "SELECT * FROM players WHERE player_key=?", ("amon ra st brown|WR",)
    ))
    other_conn.close()

    bulk.pop("updated_at"), single.pop("updated_at")
    assert bulk == single


def test_the_index_sees_players_the_bulk_path_added(mapper):
    """`resolve` reads an in-memory index; a bulk insert that forgets to mark
    it stale makes every later name lookup miss."""
    mapper.upsert_players([
        {"full_name": "Rookie Receiver", "position": "WR", "team": "SEA"}
    ])
    assert mapper.resolve(source="test", source_id="1", name="Rookie Receiver",
                          position="WR", team="SEA").player_key == "rookie receiver|WR"
