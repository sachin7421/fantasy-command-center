"""Storage-layer tests.

The SQL translation is the risky part of running one codebase on two backends:
a placeholder bug would not surface until the hosted deployment ran. These tests
pin the translation rules, and exercise the real schema on SQLite.
"""
from __future__ import annotations

import pytest

from src import db
from src.storage import (
    Database,
    _named_to_pyformat,
    connect_sqlite,
    is_postgres_url,
    to_postgres_sql,
)


# --- URL detection -----------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://user:pw@host:5432/db", True),
        ("postgres://user:pw@host:5432/db", True),
        ("postgresql+psycopg://user:pw@host/db", True),
        ("data/league.db", False),
        ("", False),
        (None, False),
    ],
)
def test_is_postgres_url(url, expected):
    assert is_postgres_url(url) is expected


# --- placeholder translation -------------------------------------------------

def test_question_marks_become_pyformat():
    sql = "INSERT INTO players(a, b, c) VALUES (?,?,?)"
    assert to_postgres_sql(sql) == "INSERT INTO players(a, b, c) VALUES (%s,%s,%s)"


def test_question_mark_inside_a_string_literal_is_left_alone():
    """A literal question mark in quoted SQL must not become a placeholder."""
    sql = "SELECT * FROM t WHERE note = 'why?' AND id = ?"
    assert to_postgres_sql(sql) == "SELECT * FROM t WHERE note = 'why?' AND id = %s"


def test_named_parameters_convert():
    sql = "SELECT * FROM projections WHERE season=:season AND week=:week"
    assert _named_to_pyformat(sql) == (
        "SELECT * FROM projections WHERE season=%(season)s AND week=%(week)s"
    )


def test_postgres_cast_syntax_is_not_mistaken_for_a_parameter():
    """`::timestamptz` is a cast, not a bind parameter."""
    sql = "SELECT fetched_at::timestamptz FROM adp WHERE player_key=:player_key"
    converted = _named_to_pyformat(sql)
    assert "::timestamptz" in converted
    assert "%(player_key)s" in converted


# --- schema ------------------------------------------------------------------

def test_schemas_declare_the_same_tables():
    """The two dialect schemas must not drift apart."""
    import re

    def tables(sql: str) -> set[str]:
        return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))

    assert tables(db.schema_for("sqlite")) == tables(db.schema_for("postgres"))


def test_sqlite_schema_has_no_postgres_only_syntax():
    sql = db.schema_for("sqlite")
    assert "BIGSERIAL" not in sql
    assert "AUTOINCREMENT" in sql


def test_postgres_schema_has_no_sqlite_only_syntax():
    sql = db.schema_for("postgres")
    assert "AUTOINCREMENT" not in sql
    assert "PRAGMA" not in sql
    assert "BIGSERIAL" in sql


def test_expected_history_tables_exist():
    """The append-only tables are what make later analysis possible."""
    sql = db.schema_for("postgres")
    for table in (
        "projection_history", "player_week_actuals", "matchups",
        "standings_history", "recommendations", "adp", "injuries", "trending",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


# --- round trip on a real database ------------------------------------------

@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "storage.db")
    yield connection
    connection.close()


def test_init_creates_every_table(conn):
    for table in ("players", "projections", "projection_history", "recommendations"):
        assert conn.table_exists(table)


def test_scalar_helper(conn):
    assert conn.scalar("SELECT COUNT(*) FROM players") == 0


def test_upsert_syntax_works_on_sqlite(conn):
    """The ON CONFLICT form replacing INSERT OR REPLACE must run on both."""
    for _ in range(2):
        conn.execute(
            "INSERT INTO adp(player_key, source, adp, stdev, best, worst, fetched_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(player_key, source, fetched_at) "
            "DO UPDATE SET adp=excluded.adp",
            ("x|WR", "fantasypros", 12.5, 1.0, 10, 15, "2026-08-24T00:00:00+00:00"),
        )
    conn.commit()
    assert conn.scalar("SELECT COUNT(*) FROM adp") == 1
    assert conn.scalar("SELECT adp FROM adp") == pytest.approx(12.5)


def test_projection_history_appends_rather_than_overwrites(conn):
    """Two observations of the same projection at different times both survive."""
    for stamp, points in (("2026-08-01T00:00:00+00:00", 250.0),
                          ("2026-08-24T00:00:00+00:00", 275.0)):
        db.record_projection_history(
            conn, "x|RB", "sleeper", 2026, 0, points, "{}", stamp
        )
    conn.commit()
    assert conn.scalar("SELECT COUNT(*) FROM projection_history") == 2
    latest = conn.fetchone(
        "SELECT points FROM projection_history ORDER BY observed_at DESC LIMIT 1"
    )
    assert latest["points"] == pytest.approx(275.0)


def test_recording_the_same_observation_twice_is_idempotent(conn):
    for _ in range(2):
        db.record_projection_history(
            conn, "x|RB", "sleeper", 2026, 0, 250.0, "{}", "2026-08-01T00:00:00+00:00"
        )
    conn.commit()
    assert conn.scalar("SELECT COUNT(*) FROM projection_history") == 1


def test_actuals_upsert_in_place(conn):
    db.record_actual(conn, "x|RB", 2026, 5, 12.0)
    db.record_actual(conn, "x|RB", 2026, 5, 18.5)  # corrected stat line
    conn.commit()
    assert conn.scalar("SELECT COUNT(*) FROM player_week_actuals") == 1
    assert conn.scalar("SELECT points FROM player_week_actuals") == pytest.approx(18.5)


def test_cache_round_trip(conn):
    db.cache_put(conn, "k", "sleeper", {"a": 1})
    payload, fetched_at = db.cache_get(conn, "k")
    assert payload == {"a": 1}
    assert fetched_at


def test_snapshot_diffing(conn):
    first = db.snapshot_put(conn, "injuries", {"p": "Out"})
    second = db.snapshot_put(conn, "injuries", {"p": "Questionable"})
    assert db.snapshot_latest(conn, "injuries") == {"p": "Questionable"}
    if first != second:
        assert db.snapshot_latest(conn, "injuries", before=second) == {"p": "Out"}


# --- migration ---------------------------------------------------------------

def test_migration_refuses_to_copy_a_database_onto_itself(tmp_path):
    from src.migrate import migrate

    path = tmp_path / "same.db"
    a = db.init_db(path)
    b = connect_sqlite(path)
    with pytest.raises(ValueError):
        migrate(a, b)
    a.close()
    b.close()


def test_migration_copies_rows_and_is_idempotent(tmp_path):
    from src.migrate import migrate

    source = db.init_db(tmp_path / "src.db")
    target = db.init_db(tmp_path / "dst.db")
    source.execute(
        "INSERT INTO players(player_key, full_name, position, team, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("jahmyr gibbs|RB", "Jahmyr Gibbs", "RB", "DET", db.utcnow()),
    )
    source.commit()

    migrate(source, target)
    assert target.scalar("SELECT COUNT(*) FROM players") == 1

    # Re-running must not duplicate.
    migrate(source, target)
    assert target.scalar("SELECT COUNT(*) FROM players") == 1

    source.close()
    target.close()


def test_database_url_prefers_environment(monkeypatch):
    from src import storage

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    assert storage.database_url() == "postgresql://u:p@h:5432/d"


def test_database_url_falls_back_to_streamlit_secrets(monkeypatch):
    """Streamlit Cloud supplies secrets via st.secrets, not os.environ.

    Reading only the environment would silently drop a hosted deployment back
    onto an empty SQLite file, which looks like total data loss.
    """
    import sys
    import types

    from src import storage

    for key in storage.DB_URL_KEYS:
        monkeypatch.delenv(key, raising=False)

    fake = types.ModuleType("streamlit")
    fake.secrets = {"DATABASE_URL": "postgresql://from:secrets@h:5432/d"}
    monkeypatch.setitem(sys.modules, "streamlit", fake)

    assert storage.database_url() == "postgresql://from:secrets@h:5432/d"


def test_database_url_none_when_nothing_configured(monkeypatch):
    import sys
    import types

    from src import storage

    for key in storage.DB_URL_KEYS:
        monkeypatch.delenv(key, raising=False)
    fake = types.ModuleType("streamlit")
    fake.secrets = {}
    monkeypatch.setitem(sys.modules, "streamlit", fake)

    assert storage.database_url() is None


def test_trailing_newline_in_a_pasted_secret_is_stripped(monkeypatch):
    """Regression: a secret pasted into a textarea carries a newline.

    Postgres then reads the database name as "postgres\n" and refuses the
    connection - a failure that appears only in the deployment, never locally.
    """
    from src import storage

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/postgres\n")
    assert storage.database_url() == "postgresql://u:p@h:5432/postgres"


def test_surrounding_quotes_are_stripped(monkeypatch):
    from src import storage

    monkeypatch.setenv("DATABASE_URL", '"postgresql://u:p@h:5432/postgres"')
    assert storage.database_url() == "postgresql://u:p@h:5432/postgres"


def test_whitespace_only_value_is_treated_as_unset(monkeypatch):
    import sys
    import types

    from src import storage

    monkeypatch.setenv("DATABASE_URL", "   \n")
    fake = types.ModuleType("streamlit")
    fake.secrets = {}
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    assert storage.database_url() is None
