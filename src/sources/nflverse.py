"""nflverse adapter: schedules, bye weeks and strength of schedule (spec 3.3).

Uses `nflreadpy` (the maintained successor to nfl_data_py, which pins
pandas<2 and numpy<2 and does not install on current Python).

Provides:
  - the season schedule, cached for the playoff SOS view (spec 6.4)
  - bye weeks per team, derived from the schedule rather than hardcoded
  - historical weekly scoring, used to calibrate week-to-week variance
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable

from src import db
from src.idmap import normalize_team
from src.sources.base import Source

log = logging.getLogger(__name__)

REGULAR_SEASON_WEEKS = 18


class NflverseSource(Source):
    name = "nflverse"

    # -- schedule ------------------------------------------------------------

    def schedule(self, season: int, force: bool = False) -> list[dict[str, Any]]:
        cache_key = f"nflverse:schedule:{season}"
        if not force:
            cached = db.cache_get(self.conn, cache_key)
            if cached is not None:
                return cached[0]
        try:
            import nflreadpy as nfl

            frame = nfl.load_schedules(seasons=[season])
            games = [
                {
                    "week": r.get("week"),
                    "game_type": r.get("game_type"),
                    "home_team": normalize_team(r.get("home_team")),
                    "away_team": normalize_team(r.get("away_team")),
                    "gameday": str(r.get("gameday") or ""),
                }
                for r in frame.iter_rows(named=True)
            ]
            db.cache_put(self.conn, cache_key, self.name, games)
            return games
        except Exception as exc:
            cached = db.cache_get(self.conn, cache_key)
            if cached is not None:
                log.warning("nflverse schedule fetch failed (%s); using cache", exc)
                return cached[0]
            log.warning("nflverse schedule unavailable: %s", exc)
            return []

    def bye_weeks(self, season: int, force: bool = False) -> dict[str, int]:
        """Each team's bye, derived from the weeks it does not appear."""
        games = self.schedule(season, force)
        if not games:
            return {}

        playing: dict[str, set[int]] = {}
        weeks_seen: set[int] = set()
        for game in games:
            if game.get("game_type") not in (None, "REG"):
                continue
            week = game.get("week")
            if week is None:
                continue
            week = int(week)
            weeks_seen.add(week)
            for team in (game.get("home_team"), game.get("away_team")):
                if team:
                    playing.setdefault(team, set()).add(week)

        regular_weeks = {w for w in weeks_seen if 1 <= w <= REGULAR_SEASON_WEEKS}
        byes: dict[str, int] = {}
        for team, weeks in playing.items():
            missing = sorted(regular_weeks - weeks)
            if len(missing) == 1:
                byes[team] = missing[0]
            elif missing:
                # More than one gap means incomplete data; take the earliest
                # rather than guessing, and let the caller notice.
                byes[team] = missing[0]
        return byes

    def sync_bye_weeks(self, season: int, force: bool = False) -> int:
        """Write bye weeks onto every player, keyed by their NFL team."""
        byes = self.bye_weeks(season, force)
        if not byes:
            return 0
        updated = 0
        for team, week in byes.items():
            cursor = self.conn.execute(
                "UPDATE players SET bye_week=? WHERE team=? AND "
                "(bye_week IS NULL OR bye_week<>?)",
                (week, team, week),
            )
            updated += cursor.rowcount or 0
        self.conn.commit()
        return updated

    # -- strength of schedule ------------------------------------------------

    def opponents_by_week(
        self, season: int, weeks: Iterable[int], force: bool = False
    ) -> dict[tuple[int, str], str]:
        wanted = set(weeks)
        out: dict[tuple[int, str], str] = {}
        for game in self.schedule(season, force):
            week = game.get("week")
            if week is None or int(week) not in wanted:
                continue
            home, away = game.get("home_team"), game.get("away_team")
            if home and away:
                out[(int(week), home)] = away
                out[(int(week), away)] = home
        return out

    # -- historical variance -------------------------------------------------

    def weekly_scoring_variance(
        self, seasons: list[int], force: bool = False
    ) -> dict[str, float]:
        """Coefficient of variation of weekly fantasy points, by position.

        Feeds the uncertainty band in src/projections.py with a measured number
        instead of the built-in prior.
        """
        cache_key = f"nflverse:variance:{'-'.join(map(str, seasons))}"
        if not force:
            cached = db.cache_get(self.conn, cache_key)
            if cached is not None:
                return cached[0]
        try:
            import nflreadpy as nfl
            import polars as pl

            stats = nfl.load_player_stats(seasons=seasons)
            cols = set(stats.columns)
            pos_col = "position" if "position" in cols else None
            pts_col = next(
                (c for c in ("fantasy_points_ppr", "fantasy_points") if c in cols), None
            )
            if not (pos_col and pts_col):
                return {}

            grouped = (
                stats.filter(pl.col(pts_col).is_not_null())
                .group_by(pos_col)
                .agg(
                    pl.col(pts_col).mean().alias("mean"),
                    pl.col(pts_col).std().alias("sd"),
                )
            )
            out = {
                r[pos_col]: round(float(r["sd"]) / float(r["mean"]), 3)
                for r in grouped.iter_rows(named=True)
                if r["mean"] and float(r["mean"]) > 1 and r["sd"]
            }
            db.cache_put(self.conn, cache_key, self.name, out)
            return out
        except Exception as exc:
            log.warning("nflverse variance calculation failed: %s", exc)
            cached = db.cache_get(self.conn, cache_key)
            return cached[0] if cached else {}

    def health(self) -> dict[str, Any]:
        try:
            import nflreadpy  # noqa: F401
        except Exception as exc:
            return {"source": self.name, "ok": False, "error": f"nflreadpy missing: {exc}"}
        return {"source": self.name, "ok": True}
