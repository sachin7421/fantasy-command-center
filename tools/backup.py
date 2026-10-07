"""Back the database up as gzipped CSV, one file per table, plus a manifest.

    python tools/backup.py                 # -> backup/<UTC timestamp>/
    python tools/backup.py --out somewhere

Through our own connection rather than pg_dump, so it works against the
hosted Postgres from a runner whose pg_dump is a different major version,
and against local SQLite the same way. The scheduled workflow runs it on
Sunday nights and keeps the result as an artifact for 30 days.

Nothing Yahoo-derived is stored in this database (API agreement, obligation
1), so there is nothing Yahoo-derived to back up. `forbidden()` is the
tripwire: a table whose name looks like a Yahoo payload stops the backup
instead of copying it to a second place.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db
from src.storage import Database

#: Table names that would mean Yahoo league state had been persisted.
_YAHOO_MARKERS = ("yahoo", "rosters", "free_agents", "transactions", "matchups", "standings")


def forbidden(table: str) -> bool:
    """A table that may not exist here, let alone be copied.

    `my_roster` is the manager's own typed-in roster and is allowed; every
    other roster-shaped name is not.
    """
    name = table.lower()
    if name == "my_roster":
        return False
    return any(marker in name for marker in _YAHOO_MARKERS)


def table_names(conn: Database) -> list[str]:
    if conn.dialect == "sqlite":
        rows = conn.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    else:
        rows = conn.fetchall(
            "SELECT tablename AS name FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
    return [r["name"] for r in rows if not str(r["name"]).startswith("sqlite_")]


def dump(conn: Database, out: Path) -> dict[str, Any]:
    """Write every table; return the manifest (also written as manifest.json)."""
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "backend": conn.dialect,
        "tables": {},
    }
    for table in table_names(conn):
        if forbidden(table):
            raise RuntimeError(
                f"table {table!r} looks like persisted Yahoo state; refusing to back it up. "
                "Run tools/check_yahoo_persistence.py."
            )
        rows = conn.fetchall(f"SELECT * FROM {table}")
        path = out / f"{table}.csv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if rows:
                keys = list(rows[0].keys())
                writer.writerow(keys)
                for row in rows:
                    writer.writerow([row[k] for k in keys])
        manifest["tables"][table] = len(rows)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=None, help="directory to write into")
    parser.add_argument("--db", default=None, help="SQLite path (default: configured backend)")
    args = parser.parse_args(argv)

    from src.config import Config

    cfg = Config.load("config.yaml")
    conn = db.init_db(args.db or cfg.db_path)
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(args.out) if args.out else Path("backup") / stamp
        manifest = dump(conn, out)
    finally:
        conn.close()
    total = sum(manifest["tables"].values())
    print(f"backup: {len(manifest['tables'])} tables, {total:,} rows -> {out}")
    for table, n in sorted(manifest["tables"].items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {table:<24} {n:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
