"""Writing many rows without paying the network for each one.

`fcc sync` writes thousands of rows a run, one statement at a time, over a link
whose round trip to Supabase measured 20ms on 19 Sep. 2,530 ADP rows are 50
seconds of pure waiting before any work happens, and there are several such
phases - which is how sync came to overrun a 20-minute job timeout and cancel
two consecutive morning runs.

psycopg 3 pipelines `executemany`, so the same rows cost one round trip's
latency rather than one each. sqlite3 has had `executemany` forever.
"""
from __future__ import annotations

import pytest

from src import db


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "batch.db", force_sqlite=True)
    yield connection
    connection.close()


def _rows(conn):
    return [
        (r["player_key"], r["full_name"])
        for r in conn.fetchall("SELECT player_key, full_name FROM players ORDER BY player_key")
    ]


def test_many_rows_are_written_in_one_call(conn):
    conn.executemany(
        "INSERT INTO players(player_key, full_name, position, updated_at) VALUES (?,?,?,?)",
        [(f"p{i}|WR", f"Player {i}", "WR", db.utcnow()) for i in range(3)],
    )
    conn.commit()
    assert _rows(conn) == [("p0|WR", "Player 0"), ("p1|WR", "Player 1"), ("p2|WR", "Player 2")]


def test_no_rows_is_a_no_op_rather_than_an_error(conn):
    """An empty source is a normal Tuesday, not a failure."""
    conn.executemany(
        "INSERT INTO players(player_key, full_name, position, updated_at) VALUES (?,?,?,?)",
        [],
    )
    conn.commit()
    assert _rows(conn) == []


def test_a_generator_is_accepted_and_fully_consumed(conn):
    """The call sites build rows in a loop; materialising twice is a bug."""
    rows = ((f"g{i}|RB", f"Gen {i}", "RB", db.utcnow()) for i in range(2))
    conn.executemany(
        "INSERT INTO players(player_key, full_name, position, updated_at) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    assert len(_rows(conn)) == 2


def test_upsert_conflicts_resolve_the_same_way_as_one_at_a_time(conn):
    """The hot loops all use ON CONFLICT ... DO UPDATE."""
    sql = (
        "INSERT INTO players(player_key, full_name, position, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(player_key) DO UPDATE SET full_name=excluded.full_name"
    )
    conn.executemany(sql, [("x|WR", "First Name", "WR", db.utcnow())])
    conn.executemany(sql, [("x|WR", "Corrected Name", "WR", db.utcnow())])
    conn.commit()
    assert _rows(conn) == [("x|WR", "Corrected Name")]


def test_postgres_translates_the_sql_once_and_hands_the_driver_every_row():
    """One translation, one executemany - not a loop wearing a batch's name."""
    from src.storage import Database

    calls: list[tuple] = []

    class _Cursor:
        def executemany(self, sql, rows):
            calls.append((sql, list(rows)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

    database = Database.__new__(Database)
    database.dialect = "postgres"
    database._conn = _Conn()

    database.executemany(
        "INSERT INTO adp(player_key, adp) VALUES (?,?)",
        [("a|WR", 1.0), ("b|RB", 2.0)],
    )
    assert len(calls) == 1
    sql, rows = calls[0]
    assert "%s" in sql and "?" not in sql
    assert rows == [("a|WR", 1.0), ("b|RB", 2.0)]
