"""Schema versioning.

`src/db.py` holds the schema as one `CREATE TABLE IF NOT EXISTS` script applied
on every connect. That is fine for creating a database and silently useless for
changing one: a new *table* appears on the next connect, but a new *column*
never does, because `IF NOT EXISTS` sees the table already there and skips the
whole statement.

Locally you never notice - a developer's `data/league.db` is often fresh and
does get the column. The hosted database is not fresh and does not, so the
failure surfaces at 07:00 inside a scheduled runner as `column "x" does not
exist`, with nobody watching.

So: the template in `db.py` stays the **baseline**, and every change after it is
a numbered file in `src/migrations/`. `apply()` records what has run in a
`schema_version` table and brings any database up to date, whichever backend it
is on.

Adding a migration
------------------
1. Write `src/migrations/0002_something.sql`. Dialect tokens ({REAL} and the
   rest) are substituted exactly as in the baseline, so one file serves both
   SQLite and Postgres.
2. Make the same change in `_SCHEMA_TEMPLATE`, so a NEW database gets it too.
3. `tests/test_schema.py` asserts those two agree. That test is the whole point:
   it is what makes "added a column to the template only" impossible.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from src.storage import Database

log = logging.getLogger("fcc.schema")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def available() -> list[tuple[int, Path]]:
    """Every migration on disk, in order."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.match(r"^(\d+)", path.name)
        if match:
            out.append((int(match.group(1)), path))
    return sorted(out)


def current_version(conn: Database) -> int:
    row = conn.fetchone("SELECT MAX(version) AS v FROM schema_version")
    return int(row["v"]) if row and row["v"] is not None else 0


#: A migration may declare a precondition the runner checks first:
#:
#:     -- if-missing-column: my_roster.played
#:     ALTER TABLE my_roster ADD COLUMN played INTEGER NOT NULL DEFAULT 0;
#:
#: ALTER TABLE ADD COLUMN is not idempotent, and the upgrade path replays every
#: migration against a schema that may already have its effects. Postgres has
#: ADD COLUMN IF NOT EXISTS; SQLite does not. Catching the duplicate-column
#: error instead would mean swallowing a failure to find out whether it
#: mattered, which is the habit this project keeps having to break.
_IF_MISSING_COLUMN = re.compile(
    r"^--\s*if-missing-column:\s*(\w+)\.(\w+)\s*$", re.MULTILINE
)


def should_run(conn: Database, sql: str) -> bool:
    """Whether a migration's precondition is met. True when it declares none."""
    match = _IF_MISSING_COLUMN.search(sql)
    if not match:
        return True
    table, column = match.group(1), match.group(2)
    try:
        return not conn.column_exists(table, column)
    except Exception as exc:
        log.warning(
            "Could not check %s.%s for migration precondition (%s); running it",
            table, column, exc,
        )
        return True


def apply(conn: Database, baseline: str) -> list[int]:
    """Bring `conn` up to date. Returns the migrations that ran.

    Three cases, and the third is the one that matters:

    * **Empty database.** Run the baseline, then stamp every known migration as
      applied - the baseline already contains their effects by construction.
    * **Known database.** Run whatever is newer than the recorded version.
    * **Existing database with no version table.** It predates versioning, so it
      is stamped at 0 and every migration runs. This is the upgrade path for the
      database already deployed.
    """
    from src.db import schema_for

    conn.executescript(_VERSION_TABLE)

    fresh = not _has_tables(conn)
    if fresh:
        conn.executescript(baseline)

    version = current_version(conn)
    migrations = available()
    ran: list[int] = []

    if fresh:
        # The baseline is defined to be current, so record the migrations as
        # already applied rather than replaying them against a schema that
        # already has their effects.
        for number, _ in migrations:
            _stamp(conn, number)
        conn.commit()
        return ran

    for number, path in migrations:
        if number <= version:
            continue
        sql = schema_for(conn.dialect, template=path.read_text(encoding="utf-8"))
        log.info("Applying migration %s", path.name)
        if should_run(conn, sql):
            conn.executescript(sql)
        else:
            log.info("migration %s precondition already satisfied; skipped", number)
        _stamp(conn, number)
        ran.append(number)

    if not fresh:
        # Anything the baseline adds that is a whole new TABLE is safe to apply
        # to an existing database, and this is how new tables reach it.
        conn.executescript(baseline)
    conn.commit()
    return ran


def _has_tables(conn: Database) -> bool:
    """Whether this database already holds the application's own tables."""
    return conn.table_exists("players")


def _stamp(conn: Database, version: int) -> None:
    from src.db import utcnow

    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (?,?) "
        "ON CONFLICT(version) DO NOTHING",
        (version, utcnow()),
    )
