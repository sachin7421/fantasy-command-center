"""Projection blending and uncertainty (spec 4.3, 4.4).

Blends per-source, league-scored projections into one number with a floor and a
ceiling. Two rules matter:

  1. Weights renormalize when a source is missing, so a player covered by only
     one source is not silently penalised.
  2. Uncertainty combines *source disagreement* with *positional volatility*.
     Disagreement alone collapses to zero when only one source has an opinion,
     which would falsely present a lone projection as a certainty.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from src import db

log = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {"fantasypros": 0.5, "yahoo": 0.25, "espn": 0.25, "sleeper": 0.5}

#: Week-to-week coefficient of variation by position, from historical scoring.
#: Used as the floor on uncertainty when sources happen to agree.
POSITION_VOLATILITY = {
    "QB": 0.28, "RB": 0.42, "WR": 0.48, "TE": 0.46, "K": 0.40, "DEF": 0.55,
}
DEFAULT_VOLATILITY = 0.45


@dataclass
class Blend:
    player_key: str
    points: float
    floor: float
    ceiling: float
    stdev: float
    n_sources: int
    detail: dict[str, float] = field(default_factory=dict)


#: Weight applied to a source that is present but absent from the config.
#: Dropping it instead would silently discard a real projection - which is how
#: a misconfigured weight map can quietly turn a blend into a single source.
UNCONFIGURED_SOURCE_WEIGHT = 0.25


def normalize_weights(
    available: Iterable[str], weights: dict[str, float]
) -> dict[str, float]:
    """Restrict weights to the sources we actually have, then renormalize.

    A source with real data is never dropped just because the config forgot to
    mention it; it falls back to a default weight and is logged once.
    """
    available = list(available)
    present: dict[str, float] = {}
    for source in available:
        weight = weights.get(source)
        if weight is None:
            log.debug(
                "No configured weight for source %r; using %.2f",
                source, UNCONFIGURED_SOURCE_WEIGHT,
            )
            weight = UNCONFIGURED_SOURCE_WEIGHT
        weight = float(weight)
        if weight > 0:
            present[source] = weight

    total = sum(present.values())
    if not total:
        n = len(available)
        return {s: 1.0 / n for s in available} if n else {}
    return {s: w / total for s, w in present.items()}


def blend_player(
    player_key: str,
    per_source: dict[str, float],
    weights: dict[str, float],
    position: str | None = None,
    band_sd: float = 1.0,
) -> Blend:
    """Combine one player's per-source points into a blended projection."""
    values = {s: v for s, v in per_source.items() if v is not None}
    if not values:
        return Blend(player_key, 0.0, 0.0, 0.0, 0.0, 0, {})

    normalized = normalize_weights(values.keys(), weights)
    points = sum(values[s] * normalized.get(s, 0.0) for s in values)

    # Disagreement between sources.
    spread = statistics.pstdev(list(values.values())) if len(values) > 1 else 0.0
    # Intrinsic week-to-week variance for the position.
    volatility = POSITION_VOLATILITY.get(position or "", DEFAULT_VOLATILITY)
    intrinsic = abs(points) * volatility * 0.5

    # Take the larger: agreement between two sources is not evidence of low risk.
    stdev = max(spread, intrinsic)
    return Blend(
        player_key=player_key,
        points=round(points, 2),
        floor=round(points - band_sd * stdev, 2),
        ceiling=round(points + band_sd * stdev, 2),
        stdev=round(stdev, 2),
        n_sources=len(values),
        detail={s: round(v, 2) for s, v in values.items()},
    )


def blend_all(
    conn: sqlite3.Connection,
    season: int,
    week: int = 0,
    weights: dict[str, float] | None = None,
    band_sd: float = 1.0,
) -> int:
    """Blend every player with at least one stored projection for the period."""
    weights = weights or DEFAULT_WEIGHTS
    rows = conn.execute(
        """
        SELECT j.player_key, j.source, j.points, p.position
        FROM projections j
        JOIN players p USING(player_key)
        WHERE j.season=? AND j.week=? AND j.points IS NOT NULL
        """,
        (season, week),
    ).fetchall()

    grouped: dict[str, dict[str, float]] = {}
    positions: dict[str, str] = {}
    for r in rows:
        grouped.setdefault(r["player_key"], {})[r["source"]] = float(r["points"])
        positions[r["player_key"]] = r["position"]

    computed_at = db.utcnow()
    written = 0
    for player_key, per_source in grouped.items():
        blend = blend_player(
            player_key, per_source, weights, positions.get(player_key), band_sd
        )
        conn.execute(
            "INSERT INTO projections_blended(player_key, season, week, points, floor, "
            "ceiling, stdev, n_sources, detail_json, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(player_key, season, week) DO UPDATE SET points=excluded.points, "
            "floor=excluded.floor, ceiling=excluded.ceiling, stdev=excluded.stdev, "
            "n_sources=excluded.n_sources, detail_json=excluded.detail_json, "
            "computed_at=excluded.computed_at",
            (
                player_key, season, week, blend.points, blend.floor, blend.ceiling,
                blend.stdev, blend.n_sources, json.dumps(blend.detail), computed_at,
            ),
        )
        written += 1
    conn.commit()
    return written


def risk_adjusted_points(
    points: float, floor: float, ceiling: float, mode: str
) -> float:
    """Tilt a projection toward floor or ceiling (spec 6.3 risk mode).

    Underdogs need variance, favourites need to avoid it.
    """
    if mode == "ceiling":
        return points + 0.5 * (ceiling - points)
    if mode == "floor":
        return points - 0.5 * (points - floor)
    return points


def resolve_risk_mode(configured: str, projected_margin: float | None) -> str:
    """`auto` becomes ceiling when projected to lose, floor when favoured."""
    if configured != "auto":
        return configured
    if projected_margin is None:
        return "neutral"
    if projected_margin < -3:
        return "ceiling"
    if projected_margin > 3:
        return "floor"
    return "neutral"
