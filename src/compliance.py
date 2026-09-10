"""Obligations under the Yahoo API agreement, in one place.

Signed 2026-09-10. The agreement is mostly enforced structurally elsewhere -
`db.cache_put` refuses the source, the client holds responses in memory, tests
assert the attribution and the read-only grant. What lives here is the part
that has to be *performed*: removing Yahoo-derived data on demand.

The boundary, stated once so it is not re-derived differently each time:

    Yahoo's        rosters, free agents, team budgets, the transaction log,
                   and any Yahoo identifier
    Ours           players, projections, blends, VORP, tiers, recommendations,
                   learned bid coefficients, and the draft picks we typed in
    The user's     league scoring rules and roster slots, in config.yaml
"""
from __future__ import annotations

import logging

from src.storage import Database

log = logging.getLogger(__name__)

#: Tables that exist only to hold Yahoo league state. Emptied entirely.
YAHOO_TABLES: tuple[str, ...] = (
    "rosters",
    "free_agents",
    "team_budgets",
    "transactions",
)

#: Columns on tables of OUR OWN that carry a Yahoo identifier. Cleared, not
#: dropped, and the rows are kept: the player is ours, only the id is Yahoo's.
YAHOO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("players", "yahoo_id"),
    ("players", "yahoo_key"),
)


def purge_yahoo(conn: Database) -> dict[str, int]:
    """Delete everything Yahoo-derived. Returns what was removed, per table.

    For use if the agreement terminates (obligation 5).

    The hard half is what this must NOT touch. Our projections, our blended
    numbers, our draft board and the draft picks typed in by hand are not
    Yahoo's, and a purge that took them would destroy the application in order
    to honour a clause about someone else's data. Every deletion below names
    exactly one table or column.

    Yahoo identifiers go even where Sleeper published them. During the
    agreement that distinction is worth drawing - a cross-reference id in
    Sleeper's own free API is Sleeper's data, and it buys an exact join. Once
    the grant has ended the safe reading is that no Yahoo identifier should
    remain, and the cost is falling back to name matching.

    Idempotent: running it twice is not an error, and the second run reports
    zeros.
    """
    removed: dict[str, int] = {}

    for table in YAHOO_TABLES:
        try:
            before = conn.scalar(f"SELECT COUNT(*) FROM {table}") or 0
        except Exception as exc:
            # A table that does not exist has nothing to purge, which is a
            # success. Reported rather than swallowed so a typo in the list
            # above cannot masquerade as a clean purge.
            log.info("purge: %s is not present (%s)", table, exc)
            removed[table] = 0
            continue
        conn.execute(f"DELETE FROM {table}")
        removed[table] = int(before)

    for table, column in YAHOO_COLUMNS:
        try:
            cleared = conn.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"
            ) or 0
            conn.execute(f"UPDATE {table} SET {column}=NULL WHERE {column} IS NOT NULL")
        except Exception as exc:
            log.info("purge: %s.%s is not present (%s)", table, column, exc)
            cleared = 0
        removed[f"{table}.{column}"] = int(cleared)

    # Yahoo may never be in source_cache at all - cache_put refuses it - but a
    # database that predates that guard can still hold rows from before.
    try:
        stale = conn.scalar(
            "SELECT COUNT(*) FROM source_cache WHERE LOWER(source) LIKE '%yahoo%'"
        ) or 0
        conn.execute("DELETE FROM source_cache WHERE LOWER(source) LIKE '%yahoo%'")
        removed["source_cache"] = int(stale)
    except Exception as exc:
        log.info("purge: source_cache not present (%s)", exc)
        removed["source_cache"] = 0

    conn.commit()
    total = sum(removed.values())
    log.warning("Yahoo purge complete: %s row(s)/value(s) removed", total)
    return removed


def describe_purge(removed: dict[str, int]) -> list[str]:
    """Human-readable evidence of what a purge did.

    A compliance action nobody can evidence is not much use, so the CLI prints
    this and it is worth keeping.
    """
    lines = []
    for name, count in sorted(removed.items()):
        lines.append(f"  {name:<24} {count:>7,}")
    lines.append(f"  {'total':<24} {sum(removed.values()):>7,}")
    return lines
