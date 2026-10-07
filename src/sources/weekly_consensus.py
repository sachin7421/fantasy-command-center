"""FantasyPros' weekly expert consensus, via nflverse's daily mirror.

`nflreadpy.load_ff_rankings("week")` carries, for ~590 players, the
consensus rank (`ecr`), its spread across experts (`sd`), a rank-to-points
value (`r2p_pts`), the opponent, the kickoff status and a start/sit grade,
scraped daily. It is the broadest consensus available for free, and until
7 Oct 2026 this project used FantasyPros for draft-season ECR only.

Scale is the catch. The points are full-PPR ("ppr-wr" pages) and this league
is half-PPR with its own fumble rules, so each position's points are
calibrated against the week's own Sleeper/ESPN blend - a least-squares
factor fitted on the overlapping players, applied to everyone at the
position. A position with fewer than `MIN_OVERLAP` overlapping players keeps
the raw number and the stored line says `calibrated: false`.

Stored as source "fantasypros" for the week, alongside the season ECR the
existing source stores under week 0. IDP pages (db, lb, dl) and kickers are
skipped: the league rosters neither.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from src import db
from src.idmap import IdMapper, normalize_team
from src.sources.base import Source

log = logging.getLogger(__name__)

#: Players at a position that must overlap the blend before its factor is trusted.
MIN_OVERLAP = 8

_PAGES = {"qb": "QB", "ppr-rb": "RB", "ppr-wr": "WR", "ppr-te": "TE", "dst": "DEF"}


@dataclass(frozen=True)
class Calibration:
    """points_here = factor * points_there, fitted through the origin."""

    factor: float
    n: int

    def apply(self, value: float) -> float:
        return round(self.factor * value, 2)


def calibrate(theirs: dict[str, float], ours: dict[str, float], minimum: int = MIN_OVERLAP) -> Calibration | None:
    """The scale factor between two sources on the players they share.

    Through the origin, because zero points is zero points on any scale, and
    one parameter is all a handful of players can support honestly.
    """
    pairs = [(theirs[k], ours[k]) for k in theirs if k in ours and theirs[k] > 0]
    if len(pairs) < minimum:
        return None
    num = sum(t * o for t, o in pairs)
    den = sum(t * t for t, o in pairs)
    if den <= 0:
        return None
    return Calibration(factor=num / den, n=len(pairs))


class WeeklyConsensusSource(Source):
    name = "fantasypros"

    def fetch_rows(self, force: bool = False) -> list[dict[str, Any]]:
        """The mirror's current weekly table. Unreachable -> [] with a warning."""
        try:
            import nflreadpy as nfl

            from src.sources.usage import _rows

            return _rows(nfl.load_ff_rankings("week"))
        except Exception as exc:
            log.warning("weekly consensus rankings unavailable: %s", exc)
            return []

    def sync(self, idmap: IdMapper, season: int, week: int, force: bool = False) -> dict[str, int]:
        """Store this week's consensus as a projection row per player."""
        stats = {"loaded": 0, "stored": 0, "unmatched": 0, "skipped_idp": 0, "calibrated_positions": 0}
        rows = self.fetch_rows(force=force)
        if not rows:
            return stats

        # Resolve first, so the calibration pairs on OUR keys.
        resolved: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            stats["loaded"] += 1
            position = _PAGES.get(str(row.get("page") or "").lower())
            if position is None:
                stats["skipped_idp"] += 1
                continue
            name = str(row.get("player_name") or "")
            team = normalize_team(row.get("team"))
            match = idmap.resolve(
                source=self.name, source_id=None, name=name, position=position, team=team,
            )
            if not match.player_key:
                stats["unmatched"] += 1
                continue
            resolved.append((match.player_key, position, row))

        blend = {
            r["player_key"]: float(r["points"])
            for r in self.conn.fetchall(
                "SELECT player_key, points FROM projections_blended WHERE season=? AND week=?",
                (season, week),
            )
        }
        fits: dict[str, Calibration | None] = {}
        for position in set(_PAGES.values()):
            theirs = {k: float(r.get("r2p_pts") or 0) for k, p, r in resolved if p == position}
            fits[position] = calibrate(theirs, blend)
            if fits[position] is not None:
                stats["calibrated_positions"] += 1

        fetched_at = db.utcnow()
        for key, position, row in resolved:
            raw = float(row.get("r2p_pts") or 0)
            fit = fits.get(position)
            points = fit.apply(raw) if fit else round(raw, 2)
            line = {
                "r2p_pts": raw,
                "ecr": _f(row.get("ecr")),
                "sd": _f(row.get("sd")),
                "opponent": row.get("player_opponent") or None,
                "grade": row.get("start_sit_grade") or None,
                "game_status": row.get("player_game_status") or None,
                "calibrated": fit is not None,
                "factor": round(fit.factor, 4) if fit else None,
            }
            self.conn.execute(
                "INSERT INTO projections(player_key, source, season, week, stats_json, "
                "points, fetched_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(player_key, source, season, week) DO UPDATE SET "
                "stats_json=excluded.stats_json, points=excluded.points, "
                "fetched_at=excluded.fetched_at",
                (key, self.name, season, week, json.dumps(line), points, fetched_at),
            )
            db.record_projection_history(
                self.conn, key, self.name, season, week, points, json.dumps(line), fetched_at,
            )
            stats["stored"] += 1
        self.conn.commit()
        return stats


def _f(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
