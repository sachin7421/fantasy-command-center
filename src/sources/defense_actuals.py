"""Ground truth for team defenses, from nflverse team stats and the game score.

`points_actual` existed for players only; `fcc verify-scoring` reported every
rostered DEF as "not-scored" on 7 Oct 2026. Two nflverse feeds supply what
Yahoo's defensive categories need:

  load_team_stats(summary_level="week")  sacks, interceptions, recoveries,
                                         touchdowns, safeties, blocks
  load_schedules                         the final score, hence points allowed

Verified against Yahoo's four listed DEF totals on the user's roster
(weeks 1-4), each reproduced exactly - see tests/test_defense_actuals.py.
`pt_returned` in the feed is NOT Yahoo's "extra point returned" and is
deliberately ignored: counting it put JAX week 1 at 15 against Yahoo's 13.
"""
from __future__ import annotations

import logging
from typing import Any

from src import db
from src.idmap import normalize_team
from src.storage import Database

log = logging.getLogger(__name__)


def _f(value: Any) -> float:
    try:
        return 0.0 if value is None else float(value)
    except (TypeError, ValueError):
        return 0.0


def defense_line(row: dict[str, Any], *, points_allowed: float) -> dict[str, float]:
    """One team-week of `load_team_stats` as a scoreable DT stat line."""
    return {
        "def_sack": _f(row.get("def_sacks")),
        "def_int": _f(row.get("def_interceptions")),
        "def_fum_rec": _f(row.get("fumble_recovery_opp")),
        "def_td": _f(row.get("def_tds")),
        "def_safety": _f(row.get("def_safeties")),
        "def_blk_kick": (
            _f(row.get("def_punt_blocks"))
            + _f(row.get("def_pat_blocks"))
            + _f(row.get("def_fg_blocks"))
        ),
        "def_ret_td": _f(row.get("special_teams_tds")),
        # A defensive two-point conversion is what Yahoo calls "Extra Point
        # Returned". Rare; verify-scoring is what will confirm the mapping
        # the first time one happens.
        "def_xpr": _f(row.get("def_2pt_made")),
        "def_pts_allowed": float(points_allowed),
    }


def points_allowed_by_week(games: list[dict[str, Any]]) -> dict[tuple[str, int], float]:
    """(team, week) -> points the opponent scored, for every finished game."""
    out: dict[tuple[str, int], float] = {}
    for game in games:
        week = game.get("week")
        home, away = game.get("home_team"), game.get("away_team")
        home_score, away_score = game.get("home_score"), game.get("away_score")
        if week is None or not home or not away:
            continue
        if home_score is None or away_score is None:
            continue  # not played yet
        try:
            out[(normalize_team(home), int(week))] = float(away_score)
            out[(normalize_team(away), int(week))] = float(home_score)
        except (TypeError, ValueError):
            continue
    return out


def sync_defense_actuals(
    conn: Database, scoring: Any, *, season: int,
    team_rows: list[dict[str, Any]], games: list[dict[str, Any]],
) -> int:
    """Store points_actual for every DEF team-week with a final score.

    A team-week with no score is skipped, not scored: a missing opponent score
    would otherwise read as a shutout and award ten points for nothing.
    """
    allowed = points_allowed_by_week(games)
    known = {
        r["team"]: r["player_key"]
        for r in conn.fetchall("SELECT team, player_key FROM players WHERE position='DEF'")
    }
    recorded_at = db.utcnow()
    rows: list[tuple] = []
    for row in team_rows:
        if row.get("season_type") not in (None, "REG") or row.get("week") is None:
            continue
        team = normalize_team(row.get("team"))
        week = int(row["week"])
        key = known.get(team)
        pts = allowed.get((team, week))
        if key is None or pts is None:
            continue
        line = defense_line(row, points_allowed=pts)
        rows.append((key, season, week, scoring.score(line, "DT"), None, "nflverse", recorded_at))
    if rows:
        db.record_actuals_many(conn, rows)
        conn.commit()
    return len(rows)


def load_and_sync(conn: Database, scoring: Any, season: int) -> int:
    """Fetch both feeds and store. Unreachable feeds are reported, not fatal."""
    try:
        import nflreadpy as nfl

        from src.sources.usage import _rows

        team_rows = _rows(nfl.load_team_stats(seasons=[season], summary_level="week"))
        games = _rows(nfl.load_schedules(seasons=[season]))
    except Exception as exc:
        log.warning("defense actuals unavailable for %s: %s", season, exc)
        return 0
    return sync_defense_actuals(conn, scoring, season=season, team_rows=team_rows, games=games)
