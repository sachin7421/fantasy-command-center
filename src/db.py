"""SQLite persistence layer for the Fantasy Command Center.

Single connection factory + schema bootstrap. Every external fetch lands here
with a `fetched_at` timestamp so jobs can degrade to cache when a source is down
(spec 3, design rule).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path("data/league.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Canonical player identity, cross-mapped across sources (spec 7).
CREATE TABLE IF NOT EXISTS players (
    player_key   TEXT PRIMARY KEY,          -- our canonical id (normalized name|pos)
    yahoo_id     TEXT,
    yahoo_key    TEXT,                      -- e.g. "449.p.12345"
    sleeper_id   TEXT,
    gsis_id      TEXT,                      -- nflverse
    espn_id      TEXT,
    full_name    TEXT NOT NULL,
    first_name   TEXT,
    last_name    TEXT,
    position     TEXT,
    team         TEXT,
    bye_week     INTEGER,
    status       TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_yahoo   ON players(yahoo_id);
CREATE INDEX IF NOT EXISTS idx_players_sleeper ON players(sleeper_id);
CREATE INDEX IF NOT EXISTS idx_players_name    ON players(full_name);

-- Fuzzy/manual id resolution audit trail (spec 7: idmap is a known pain point).
CREATE TABLE IF NOT EXISTS player_id_map (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    player_key   TEXT NOT NULL,
    method       TEXT,                      -- exact|alias|fuzzy|manual
    confidence   REAL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

-- Raw stat line + league-scored points, one row per (player, source, period).
CREATE TABLE IF NOT EXISTS projections (
    player_key   TEXT NOT NULL,
    source       TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,          -- 0 => full-season projection
    stats_json   TEXT NOT NULL,             -- raw stat line exactly as fetched
    points       REAL,                      -- computed with OUR league scoring
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (player_key, source, season, week)
);
CREATE INDEX IF NOT EXISTS idx_proj_lookup ON projections(season, week, source);

-- Blended projection with uncertainty band (spec 4.3, 4.4).
CREATE TABLE IF NOT EXISTS projections_blended (
    player_key   TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,          -- 0 => full-season
    points       REAL NOT NULL,
    floor        REAL,
    ceiling      REAL,
    stdev        REAL,
    n_sources    INTEGER,
    detail_json  TEXT,                      -- per-source contributions, for transparency
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (player_key, season, week)
);

CREATE TABLE IF NOT EXISTS injuries (
    player_key   TEXT NOT NULL,
    status       TEXT,                      -- Out|Doubtful|Questionable|IR|PUP|Healthy
    practice     TEXT,
    body_part    TEXT,
    note         TEXT,
    source       TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (player_key, source, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_injuries_player ON injuries(player_key, observed_at);

CREATE TABLE IF NOT EXISTS adp (
    player_key   TEXT NOT NULL,
    source       TEXT NOT NULL,
    adp          REAL,
    stdev        REAL,
    best         REAL,
    worst        REAL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (player_key, source, fetched_at)
);

CREATE TABLE IF NOT EXISTS league_settings (
    league_key    TEXT PRIMARY KEY,
    season        INTEGER,
    settings_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rosters (
    league_key   TEXT NOT NULL,
    team_key     TEXT NOT NULL,
    team_name    TEXT,
    player_key   TEXT NOT NULL,
    selected_pos TEXT,
    week         INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (league_key, team_key, player_key, week)
);

CREATE TABLE IF NOT EXISTS transactions (
    league_key   TEXT NOT NULL,
    txn_id       TEXT NOT NULL,
    type         TEXT,
    timestamp    TEXT,
    payload_json TEXT,
    PRIMARY KEY (league_key, txn_id)
);

CREATE TABLE IF NOT EXISTS free_agents (
    league_key   TEXT NOT NULL,
    player_key   TEXT NOT NULL,
    pct_owned    REAL,
    week         INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (league_key, player_key, week)
);

-- Draft results, refreshed by the live polling loop (spec 5.2).
CREATE TABLE IF NOT EXISTS draft_picks (
    league_key   TEXT NOT NULL,
    pick         INTEGER NOT NULL,
    round        INTEGER,
    team_key     TEXT,
    player_key   TEXT,
    source       TEXT DEFAULT 'yahoo',      -- yahoo|manual
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (league_key, pick)
);

-- Trending adds/drops from Sleeper (leading waiver indicator, spec 3.2).
CREATE TABLE IF NOT EXISTS trending (
    player_key     TEXT NOT NULL,
    kind           TEXT NOT NULL,           -- add|drop
    count          INTEGER,
    lookback_hours INTEGER,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (player_key, kind, fetched_at)
);

-- Output of every job, for dedup + audit (spec 6, 6.6).
CREATE TABLE IF NOT EXISTS recommendations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job          TEXT NOT NULL,
    season       INTEGER,
    week         INTEGER,
    payload_json TEXT NOT NULL,
    dedup_key    TEXT,                      -- identical key => already notified
    created_at   TEXT NOT NULL,
    notified_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_recs_dedup ON recommendations(job, dedup_key);

-- Generic point-in-time blobs for diffing (injury monitor, spec 6.2).
CREATE TABLE IF NOT EXISTS snapshots (
    kind         TEXT NOT NULL,
    taken_at     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (kind, taken_at)
);

-- Raw fetch cache so a dead source degrades to last-known-good, never crashes.
CREATE TABLE IF NOT EXISTS source_cache (
    cache_key    TEXT PRIMARY KEY,
    source       TEXT,
    payload_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
"""


def utcnow() -> str:
    """ISO-8601 UTC timestamp; the single time format used throughout the DB."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(
    db_path: str | Path = DEFAULT_DB_PATH, *, same_thread: bool = True
) -> sqlite3.Connection:
    """Open the database.

    `same_thread=False` is needed by Streamlit, which runs each script rerun on
    a fresh thread while holding one cached connection. That is safe here: this
    is a single-user local app, writes are short, and SQLite serializes them
    with its own locking (WAL is enabled in the schema).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(
    db_path: str | Path = DEFAULT_DB_PATH, *, same_thread: bool = True
) -> sqlite3.Connection:
    conn = connect(db_path, same_thread=same_thread)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- cache helpers -----------------------------------------------------------

def cache_put(conn: sqlite3.Connection, cache_key: str, source: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO source_cache(cache_key, source, payload_json, fetched_at) "
        "VALUES (?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
        "payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
        (cache_key, source, json.dumps(payload), utcnow()),
    )
    conn.commit()


def cache_get(conn: sqlite3.Connection, cache_key: str) -> tuple[Any, str] | None:
    """Return (payload, fetched_at) or None. Callers decide the staleness policy."""
    row = conn.execute(
        "SELECT payload_json, fetched_at FROM source_cache WHERE cache_key=?", (cache_key,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload_json"]), row["fetched_at"]


def snapshot_put(conn: sqlite3.Connection, kind: str, payload: Any) -> str:
    ts = utcnow()
    conn.execute(
        "INSERT OR REPLACE INTO snapshots(kind, taken_at, payload_json) VALUES (?,?,?)",
        (kind, ts, json.dumps(payload)),
    )
    conn.commit()
    return ts


def snapshot_latest(
    conn: sqlite3.Connection, kind: str, before: str | None = None
) -> Any | None:
    if before:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE kind=? AND taken_at<? "
            "ORDER BY taken_at DESC LIMIT 1",
            (kind, before),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE kind=? ORDER BY taken_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None
