"""Schema versioning.

The failure this guards against: `CREATE TABLE IF NOT EXISTS` will never add a
COLUMN to a table that already exists. So changing the schema used to work
perfectly on a fresh local database and do nothing at all to the deployed one,
surfacing as `column "x" does not exist` inside a scheduled job at 07:00.
"""
from __future__ import annotations

import pytest

from src import db, schema


def test_a_new_database_is_stamped_current(tmp_path):
    conn = db.init_db(tmp_path / "fresh.db")
    known = [n for n, _ in schema.available()]
    assert schema.current_version(conn) == (max(known) if known else 0)


def test_an_unversioned_database_is_brought_forward(tmp_path):
    """The upgrade path for the database that is already deployed."""
    path = tmp_path / "old.db"
    conn = db.init_db(path)
    # Simulate a database that predates versioning.
    conn.execute("DELETE FROM schema_version")
    conn.commit()
    assert schema.current_version(conn) == 0

    ran = schema.apply(conn, db.schema_for(conn.dialect))
    known = [n for n, _ in schema.available()]
    assert ran == known
    assert schema.current_version(conn) == (max(known) if known else 0)


def test_applying_twice_is_a_no_op(tmp_path):
    conn = db.init_db(tmp_path / "twice.db")
    before = schema.current_version(conn)
    assert schema.apply(conn, db.schema_for(conn.dialect)) == []
    assert schema.current_version(conn) == before


def test_the_baseline_and_the_migration_chain_agree(tmp_path):
    """A database built from the baseline must match one built by migrating.

    This is the test that makes "added the column to the template and forgot
    the migration" impossible. Without it the two drift, and the drift is only
    visible on a database old enough to matter.
    """
    from_template = db.init_db(tmp_path / "template.db")

    migrated = db.init_db(tmp_path / "migrated.db")
    migrated.execute("DELETE FROM schema_version")
    migrated.commit()
    schema.apply(migrated, db.schema_for(migrated.dialect))

    assert _columns(from_template) == _columns(migrated)


def _columns(conn) -> dict[str, list[str]]:
    tables = [
        r["name"] for r in conn.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        table: sorted(
            r["name"] for r in conn.fetchall(f"PRAGMA table_info({table})")
        )
        for table in tables
    }


def test_every_migration_filename_is_numbered():
    for number, path in schema.available():
        assert number > 0, path.name
        assert path.name[:4].isdigit(), path.name


def test_the_migration_copies_every_table_in_the_schema():
    """`fcc migrate` reported success while silently dropping seven tables.

    TABLES was hand-maintained beside a schema that grew, so a migration to a
    new Postgres left player_week_usage, team_budgets, practice_reports,
    depth_charts, game_context, source_accuracy and recommendation_outcomes
    behind - including the append-only history the project exists to
    accumulate - and said nothing.
    """
    import re
    from pathlib import Path

    from src.migrate import NOT_MIGRATED, TABLES

    source = Path("src/db.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", source))
    listed = {name for name, _ in TABLES}

    missing = declared - listed - NOT_MIGRATED
    assert not missing, f"declared in the schema but never migrated: {sorted(missing)}"

    unknown = listed - declared
    assert not unknown, f"migrated but not in the schema: {sorted(unknown)}"
