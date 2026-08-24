"""Schema and persistence helpers.

Runs on SQLite locally and Postgres when hosted (see src/storage.py). The DDL
below is written once with a few dialect tokens substituted per backend, so the
two schemas cannot drift apart.

Timestamps are stored as ISO-8601 UTC **text** rather than a native timestamp
type. That keeps ordering and comparison identical on both backends (ISO-8601
sorts lexicographically), and Postgres can still cast for analysis:
`SELECT fetched_at::timestamptz ...`.

Two classes of table:

  * **current state** - `players`, `projections`, `rosters`, ... upserted in
    place, holding what is true right now. The jobs read these.
  * **append-only history** - `projection_history`, `adp`, `injuries`,
    `trending`, `player_week_actuals`, `recommendations`, ... never overwritten,
    so the season accumulates into a record that can be analysed later: how
    projections drifted, how ADP moved, what was recommended, and what actually
    happened.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.storage import Database, connect as _connect, database_url, is_postgres_url

DEFAULT_DB_PATH = Path("data/league.db")

# Dialect-specific fragments substituted into the single schema below.
_DIALECT_TOKENS = {
    "sqlite": {
        "PROLOGUE": "PRAGMA journal_mode=WAL;\nPRAGMA foreign_keys=ON;",
        "SERIAL_PK": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "REAL": "REAL",
    },
    "postgres": {
        "PROLOGUE": "",
        "SERIAL_PK": "BIGSERIAL PRIMARY KEY",
        "REAL": "DOUBLE PRECISION",
    },
}

_SCHEMA_TEMPLATE = """
{PROLOGUE}

-- ============================ current state =============================

-- Canonical player identity, cross-mapped across sources (spec 7).
CREATE TABLE IF NOT EXISTS players (
    player_key   TEXT PRIMARY KEY,
    yahoo_id     TEXT,
    yahoo_key    TEXT,
    sleeper_id   TEXT,
    gsis_id      TEXT,
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

CREATE TABLE IF NOT EXISTS player_id_map (
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    player_key   TEXT NOT NULL,
    method       TEXT,
    confidence   {REAL},
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

-- Latest projection per (player, source, period).
CREATE TABLE IF NOT EXISTS projections (
    player_key   TEXT NOT NULL,
    source       TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    stats_json   TEXT NOT NULL,
    points       {REAL},
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (player_key, source, season, week)
);
CREATE INDEX IF NOT EXISTS idx_proj_lookup ON projections(season, week, source);

CREATE TABLE IF NOT EXISTS projections_blended (
    player_key   TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    points       {REAL} NOT NULL,
    floor        {REAL},
    ceiling      {REAL},
    stdev        {REAL},
    n_sources    INTEGER,
    detail_json  TEXT,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (player_key, season, week)
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
    pct_owned    {REAL},
    week         INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (league_key, player_key, week)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    league_key   TEXT NOT NULL,
    pick         INTEGER NOT NULL,
    round        INTEGER,
    team_key     TEXT,
    player_key   TEXT,
    source       TEXT DEFAULT 'yahoo',
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (league_key, pick)
);

CREATE TABLE IF NOT EXISTS source_cache (
    cache_key    TEXT PRIMARY KEY,
    source       TEXT,
    payload_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

-- ========================== append-only history =========================
-- Never updated in place. This is the material for later analysis.

-- Every projection ever observed, so drift over the season is recoverable
-- (e.g. which source moved first on a breakout, and who was right).
CREATE TABLE IF NOT EXISTS projection_history (
    player_key   TEXT NOT NULL,
    source       TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    points       {REAL},
    stats_json   TEXT,
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (player_key, source, season, week, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_proj_hist ON projection_history(season, week, observed_at);

-- What a player actually scored, under our league rules. The counterpart to
-- projection_history: together they measure how good any source really is.
CREATE TABLE IF NOT EXISTS player_week_actuals (
    player_key   TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    points       {REAL},
    stats_json   TEXT,
    source       TEXT NOT NULL DEFAULT 'yahoo',
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (player_key, season, week, source)
);
CREATE INDEX IF NOT EXISTS idx_actuals ON player_week_actuals(season, week);

CREATE TABLE IF NOT EXISTS injuries (
    player_key   TEXT NOT NULL,
    status       TEXT,
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
    adp          {REAL},
    stdev        {REAL},
    best         {REAL},
    worst        {REAL},
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (player_key, source, fetched_at)
);

CREATE TABLE IF NOT EXISTS trending (
    player_key     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    count          INTEGER,
    lookback_hours INTEGER,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (player_key, kind, fetched_at)
);

-- Weekly matchup results, for season-over-season review.
CREATE TABLE IF NOT EXISTS matchups (
    league_key    TEXT NOT NULL,
    season        INTEGER NOT NULL,
    week          INTEGER NOT NULL,
    team_key      TEXT NOT NULL,
    opponent_key  TEXT,
    points        {REAL},
    opponent_points {REAL},
    result        TEXT,
    recorded_at   TEXT NOT NULL,
    PRIMARY KEY (league_key, season, week, team_key)
);

CREATE TABLE IF NOT EXISTS standings_history (
    league_key   TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    team_key     TEXT NOT NULL,
    team_name    TEXT,
    rank         INTEGER,
    wins         INTEGER,
    losses       INTEGER,
    ties         INTEGER,
    points_for   {REAL},
    points_against {REAL},
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (league_key, season, week, team_key)
);

-- Every recommendation the system produced, kept so its advice can be graded
-- later against what actually happened.
CREATE TABLE IF NOT EXISTS recommendations (
    id           {SERIAL_PK},
    job          TEXT NOT NULL,
    season       INTEGER,
    week         INTEGER,
    payload_json TEXT NOT NULL,
    dedup_key    TEXT,
    created_at   TEXT NOT NULL,
    notified_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_recs_dedup ON recommendations(job, dedup_key);

-- ===================== usage, opportunity, context ======================
-- Everything below is *inputs to a forecast* rather than a forecast itself.
-- Opportunity is far stickier week to week than fantasy points, so these are
-- what the models actually lean on.

-- Expected fantasy points from nflverse ff_opportunity. The gap between actual
-- and expected is the regression signal: efficiency reverts, usage persists.
CREATE TABLE IF NOT EXISTS player_week_usage (
    player_key     TEXT NOT NULL,
    season         INTEGER NOT NULL,
    week           INTEGER NOT NULL,
    team           TEXT,
    -- volume
    pass_attempts  {REAL},
    rush_attempts  {REAL},
    targets        {REAL},
    receptions     {REAL},
    -- share of the team's opportunity (the sticky part)
    target_share   {REAL},
    rush_share     {REAL},
    snap_pct       {REAL},
    air_yards      {REAL},
    -- league-scored actual vs expected
    points_actual  {REAL},
    points_expected {REAL},
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (player_key, season, week)
);
CREATE INDEX IF NOT EXISTS idx_usage_period ON player_week_usage(season, week);

-- Official NFL injury report. Unlike the Sleeper feed this carries PRACTICE
-- participation, which is the part that predicts Sunday availability.
CREATE TABLE IF NOT EXISTS practice_reports (
    player_key      TEXT NOT NULL,
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    report_status   TEXT,
    practice_status TEXT,
    primary_injury  TEXT,
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (player_key, season, week)
);

-- Real depth charts, so handcuffs are looked up rather than guessed at.
CREATE TABLE IF NOT EXISTS depth_charts (
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    team         TEXT NOT NULL,
    position     TEXT NOT NULL,
    depth_rank   INTEGER NOT NULL,
    player_key   TEXT,
    player_name  TEXT,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (season, week, team, position, depth_rank)
);

-- Betting market context. Implied team total is the sharpest public forecast of
-- how many points a team will score, and the spread sets the game script.
CREATE TABLE IF NOT EXISTS game_context (
    season         INTEGER NOT NULL,
    week           INTEGER NOT NULL,
    team           TEXT NOT NULL,
    opponent       TEXT,
    is_home        INTEGER,
    spread         {REAL},
    total          {REAL},
    implied_total  {REAL},
    wind_mph       {REAL},
    temp_f         {REAL},
    precip_pct     {REAL},
    is_dome        INTEGER,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (season, week, team)
);

-- How accurate each projection source turned out to be, for OUR scoring.
-- Lets the blend weights be earned rather than assumed.
CREATE TABLE IF NOT EXISTS source_accuracy (
    source       TEXT NOT NULL,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    position     TEXT NOT NULL,
    n            INTEGER,
    mae          {REAL},
    rmse         {REAL},
    bias         {REAL},
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (source, season, week, position)
);

-- Did the advice work? Every recommendation is already logged; this records
-- what happened afterwards so the tool can be graded rather than trusted.
CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    recommendation_id INTEGER NOT NULL,
    subject_key       TEXT NOT NULL,
    kind              TEXT,
    projected         {REAL},
    actual            {REAL},
    followed          INTEGER,
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (recommendation_id, subject_key)
);

CREATE TABLE IF NOT EXISTS snapshots (
    kind         TEXT NOT NULL,
    taken_at     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (kind, taken_at)
);
"""


def schema_for(dialect: str) -> str:
    tokens = _DIALECT_TOKENS[dialect]
    return _SCHEMA_TEMPLATE.format(**tokens)


#: Kept for callers that still reference the SQLite schema directly.
SCHEMA = schema_for("sqlite")


def utcnow() -> str:
    """ISO-8601 UTC timestamp; the single time format used throughout."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- connection --------------------------------------------------------------

def connect(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    same_thread: bool = True,
    url: str | None = None,
) -> Database:
    return _connect(db_path, url=url, same_thread=same_thread)


def init_db(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    same_thread: bool = True,
    url: str | None = None,
) -> Database:
    conn = connect(db_path, same_thread=same_thread, url=url)
    conn.executescript(schema_for(conn.dialect))
    return conn


@contextmanager
def session(db_path: str | Path = DEFAULT_DB_PATH, **kwargs) -> Iterator[Database]:
    conn = init_db(db_path, **kwargs)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def describe_backend() -> str:
    url = database_url()
    if is_postgres_url(url):
        # Never echo credentials; show only the host.
        host = url.split("@")[-1].split("/")[0] if "@" in url else "postgres"
        return f"postgres ({host})"
    return f"sqlite ({DEFAULT_DB_PATH})"


# --- cache helpers -----------------------------------------------------------

def cache_put(conn: Database, cache_key: str, source: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO source_cache(cache_key, source, payload_json, fetched_at) "
        "VALUES (?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
        "payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
        (cache_key, source, json.dumps(payload), utcnow()),
    )
    conn.commit()


def cache_get(conn: Database, cache_key: str) -> tuple[Any, str] | None:
    """Return (payload, fetched_at) or None. Callers decide staleness policy."""
    row = conn.fetchone(
        "SELECT payload_json, fetched_at FROM source_cache WHERE cache_key=?", (cache_key,)
    )
    if row is None:
        return None
    return json.loads(row["payload_json"]), row["fetched_at"]


def snapshot_put(conn: Database, kind: str, payload: Any) -> str:
    ts = utcnow()
    conn.execute(
        "INSERT INTO snapshots(kind, taken_at, payload_json) VALUES (?,?,?) "
        "ON CONFLICT(kind, taken_at) DO UPDATE SET payload_json=excluded.payload_json",
        (kind, ts, json.dumps(payload)),
    )
    conn.commit()
    return ts


def snapshot_latest(conn: Database, kind: str, before: str | None = None) -> Any | None:
    if before:
        row = conn.fetchone(
            "SELECT payload_json FROM snapshots WHERE kind=? AND taken_at<? "
            "ORDER BY taken_at DESC LIMIT 1",
            (kind, before),
        )
    else:
        row = conn.fetchone(
            "SELECT payload_json FROM snapshots WHERE kind=? ORDER BY taken_at DESC LIMIT 1",
            (kind,),
        )
    return json.loads(row["payload_json"]) if row else None


# --- history helpers ---------------------------------------------------------

def record_projection_history(
    conn: Database,
    player_key: str,
    source: str,
    season: int,
    week: int,
    points: float | None,
    stats_json: str | None,
    observed_at: str | None = None,
) -> None:
    """Append an immutable observation of a projection."""
    conn.execute(
        "INSERT INTO projection_history(player_key, source, season, week, points, "
        "stats_json, observed_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(player_key, source, season, week, observed_at) DO NOTHING",
        (player_key, source, season, week, points, stats_json, observed_at or utcnow()),
    )


def record_actual(
    conn: Database,
    player_key: str,
    season: int,
    week: int,
    points: float | None,
    stats_json: str | None = None,
    source: str = "yahoo",
) -> None:
    conn.execute(
        "INSERT INTO player_week_actuals(player_key, season, week, points, stats_json, "
        "source, recorded_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(player_key, season, week, source) DO UPDATE SET "
        "points=excluded.points, stats_json=excluded.stats_json, "
        "recorded_at=excluded.recorded_at",
        (player_key, season, week, points, stats_json, source, utcnow()),
    )
