"""Copy a local SQLite database into Postgres.

Used once when moving to the hosted deployment, and safe to re-run: every table
is written with ON CONFLICT DO NOTHING, so re-running tops up rather than
duplicating. The append-only history tables therefore accumulate correctly
across repeated runs.

Order matters only loosely here (there are no foreign keys), but players are
copied first so anything inspecting the database mid-migration sees a sensible
partial state.
"""
from __future__ import annotations

import logging
from typing import Any
from collections.abc import Iterable, Sequence

from src.storage import Database

log = logging.getLogger(__name__)

#: Copied in this order. Primary keys are needed for the ON CONFLICT targets.
TABLES: list[tuple[str, tuple[str, ...]]] = [
    ("players", ("player_key",)),
    ("player_id_map", ("source", "source_id")),
    ("league_settings", ("league_key",)),
    ("projections", ("player_key", "source", "season", "week")),
    ("projections_blended", ("player_key", "season", "week")),
    ("projection_history", ("player_key", "source", "season", "week", "observed_at")),
    ("player_week_actuals", ("player_key", "season", "week", "source")),
    ("adp", ("player_key", "source", "fetched_at")),
    ("injuries", ("player_key", "source", "observed_at")),
    ("trending", ("player_key", "kind", "fetched_at")),
    ("rosters", ("league_key", "team_key", "player_key", "week")),
    ("free_agents", ("league_key", "player_key", "week")),
    ("transactions", ("league_key", "txn_id")),
    ("draft_picks", ("league_key", "pick")),
    ("matchups", ("league_key", "season", "week", "team_key")),
    ("standings_history", ("league_key", "season", "week", "team_key")),
    ("snapshots", ("kind", "taken_at")),
    ("source_cache", ("cache_key",)),
    ("team_budgets", ("league_key", "season", "team_key")),
    ("player_week_usage", ("player_key", "season", "week")),
    ("practice_reports", ("player_key", "season", "week")),
    ("depth_charts", ("season", "week", "team", "position", "depth_rank")),
    ("game_context", ("season", "week", "team")),
    ("source_accuracy", ("source", "season", "week", "position")),
    ("recommendation_outcomes", ("recommendation_id", "subject_key")),
    # These two have generated ids; copied without them so the target assigns
    # its own and no sequence collision is possible.
    ("recommendations", ()),
    ("job_runs", ()),
]

#: Tables deliberately NOT copied, with the reason. `schema_version` belongs to
#: the target database, not the source - copying it would claim the target has
#: run migrations it has not.
NOT_MIGRATED = {"schema_version"}

# Postgres allows at most 65535 bind parameters per statement, so the batch is
# sized so that (BATCH x widest table) stays comfortably under that ceiling.
BATCH = 400


def _columns(conn: Database, table: str) -> list[str]:
    if conn.dialect == "sqlite":
        rows = conn.fetchall(f"PRAGMA table_info({table})")
        return [r["name"] for r in rows]
    rows = conn.fetchall(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    )
    return [r["column_name"] for r in rows]


def _row_values(row: Any, columns: Sequence[str]) -> tuple:
    if isinstance(row, dict):
        return tuple(row.get(c) for c in columns)
    return tuple(row[c] for c in columns)


def copy_table(
    source: Database,
    target: Database,
    table: str,
    conflict_keys: tuple[str, ...],
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy one table. Returns {read, written}."""
    if not source.table_exists(table):
        return {"read": 0, "written": 0}

    source_cols = _columns(source, table)
    target_cols = set(_columns(target, table))
    # Only carry columns the target actually has, so a schema that has moved on
    # does not abort the whole migration.
    missing = [c for c in source_cols if c not in target_cols]
    if missing:
        # Silently dropping data was the old behaviour, on the theory that a
        # target "whose schema has moved on" should not abort the run. Now that
        # the schema is versioned, a column the target lacks means the target
        # has not been migrated - which is an error, not something to work
        # around by discarding the column.
        raise RuntimeError(
            f"{table}: target is missing column(s) {', '.join(missing)}. "
            "Run the migrations against it first (any `fcc` command does this "
            "on connect)."
        )
    columns = list(source_cols)
    if table == "recommendations":
        columns = [c for c in columns if c != "id"]
    if not columns:
        return {"read": 0, "written": 0}

    column_list = ", ".join(columns)
    conflict = (
        f" ON CONFLICT ({', '.join(conflict_keys)}) DO NOTHING" if conflict_keys else ""
    )
    row_placeholder = "(" + ", ".join("?" for _ in columns) + ")"

    read = written = 0
    cursor = source.execute(f"SELECT {column_list} FROM {table}")
    batch: list[tuple] = []

    def flush() -> int:
        """Write the batch as ONE multi-row INSERT.

        A row-at-a-time loop costs a network round trip per row, which against a
        hosted database turns 30k rows into many minutes. Batching the VALUES
        list cuts that to one round trip per BATCH rows.
        """
        nonlocal batch
        if not batch:
            return 0
        count = len(batch)
        if dry_run:
            batch = []
            return 0
        values_sql = ", ".join(row_placeholder for _ in batch)
        flat: list[Any] = [v for row in batch for v in row]
        target.execute(
            f"INSERT INTO {table} ({column_list}) VALUES {values_sql}{conflict}", flat
        )
        target.commit()
        batch = []
        return count

    for row in cursor:
        read += 1
        batch.append(_row_values(row, columns))
        if len(batch) >= BATCH:
            written += flush()
    written += flush()
    return {"read": read, "written": written}


def migrate(
    source: Database,
    target: Database,
    tables: Iterable[tuple[str, tuple[str, ...]]] = TABLES,
    dry_run: bool = False,
    progress=None,
) -> dict[str, dict[str, int]]:
    """Copy every table from `source` into `target`."""
    if source.dialect == target.dialect and source.url == target.url:
        raise ValueError("Source and target are the same database.")

    results: dict[str, dict[str, int]] = {}
    for table, keys in tables:
        stats = copy_table(source, target, table, keys, dry_run)
        results[table] = stats
        if progress:
            progress(table, stats)
    return results
