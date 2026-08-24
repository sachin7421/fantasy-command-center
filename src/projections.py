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
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from src import db

log = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {"fantasypros": 0.5, "yahoo": 0.25, "espn": 0.25, "sleeper": 0.5}

#: Week-to-week coefficient of variation by position: the standard deviation of
#: a player's weekly points divided by his mean, averaged across players.
#:
#: MEASURED from 2025 nflverse data, not estimated. The values here replace an
#: earlier set of guesses that were badly low - guessed RB 0.42 against a
#: measured 0.75, WR 0.48 against 0.81. Understating this by that much makes
#: every floor/ceiling band too narrow and every win probability too confident,
#: so it is worth measuring rather than assuming.
#:
#: Computed per player and then averaged, NOT pooled across players: a pooled
#: variance would mostly measure the gap between stars and scrubs, whereas a
#: floor/ceiling band needs to know how much ONE player swings week to week.
POSITION_VOLATILITY = {
    "QB": 0.478, "RB": 0.752, "WR": 0.814, "TE": 0.817,
    # No 2025 sample worth trusting for these two in this league (no kicker slot,
    # and defenses are streamed), so they keep conservative estimates.
    "K": 0.55, "DEF": 0.65,
}
DEFAULT_VOLATILITY = 0.75

#: Games a player is projected over for a full-season number.
REGULAR_SEASON_GAMES = 17


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


def effective_volatility(position: str | None, week: int = 0) -> float:
    """Coefficient of variation for the period being projected.

    POSITION_VOLATILITY is a WEEKLY figure. A season total is the sum of about
    seventeen weeks, and summing independent draws grows the standard deviation
    by sqrt(n) while the mean grows by n - so the relative spread of a season
    projection is smaller by sqrt(n), not equal to the weekly one.

    Applying the weekly figure to a season total (as this did originally) put a
    250-point running back somewhere between 156 and 344, a plus-or-minus 38%
    band that no season projection deserves.
    """
    weekly = POSITION_VOLATILITY.get(position or "", DEFAULT_VOLATILITY)
    if week and week > 0:
        return weekly
    return weekly / math.sqrt(REGULAR_SEASON_GAMES)


def blend_player(
    player_key: str,
    per_source: dict[str, float],
    weights: dict[str, float],
    position: str | None = None,
    band_sd: float = 1.0,
    week: int = 0,
) -> Blend:
    """Combine one player's per-source points into a blended projection."""
    values = {s: v for s, v in per_source.items() if v is not None}
    if not values:
        return Blend(player_key, 0.0, 0.0, 0.0, 0.0, 0, {})

    normalized = normalize_weights(values.keys(), weights)
    points = sum(values[s] * normalized.get(s, 0.0) for s in values)

    # Two INDEPENDENT sources of uncertainty, so they add in quadrature rather
    # than one being taken over the other:
    #
    #   1. disagreement between forecasters about this player
    #   2. the intrinsic week-to-week randomness of the position
    #
    # Taking max() instead - as this did originally - silently discards
    # whichever is smaller. Once the volatility figures were measured rather
    # than guessed, the intrinsic term dominated everywhere and source
    # disagreement stopped affecting the band at all.
    spread = statistics.pstdev(list(values.values())) if len(values) > 1 else 0.0

    intrinsic = abs(points) * effective_volatility(position, week)
    stdev = math.sqrt(spread**2 + intrinsic**2)
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
            player_key, per_source, weights, positions.get(player_key), band_sd,
            week=week,
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
