"""What last season says about this one.

Two distinct uses, easy to conflate:

1. **Priors for in-season estimates.** In week 3 a player's own prior-year usage
   is a far better prior than a positional average. Shrinkage needs somewhere to
   shrink *to*, and this is it.

2. **Draft-board regression flags.** A player whose prior season was inflated by
   touchdown luck is priced on that season, and the market has not discounted
   it. The reverse is where value hides.

The correction that makes (2) honest
------------------------------------
Expected-points models are built on **league-average efficiency**. A genuinely
elite player beats his expected points every year - that is skill, not luck.
Reading the raw residual naively tells you to fade every star, which is wrong and
would have had you avoiding the best players in football.

So the residual is measured **relative to the positional norm**, as a z-score:

    z = (player residual - position mean residual) / position sd

A z of +0.3 for a top-five back is business as usual. A z of +2 is a flag. And
where several seasons exist, a residual that *persists* is evidence of skill and
is discounted further - it is only the recent, unrepeated spike that regresses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Any

from src.storage import Database

#: Games required in the prior season before it says anything.
MIN_GAMES = 8
#: |z| beyond which a player is worth flagging on the draft board.
Z_FLAG = 1.25

#: How much of a prior-season residual carries into the next season.
#:
#: MEASURED, not assumed. Correlating each player's points-over-expected across
#: consecutive seasons of nflverse opportunity data:
#:
#:     2023 -> 2024   r = +0.202  (n=208)
#:     2024 -> 2025   r = +0.226  (n=202)
#:     2023 -> 2025   r = +0.243  (n=169)
#:
#: So roughly a fifth of the gap repeats and about four fifths evaporates: there
#: IS a persistent skill component - elite players really do beat league-average
#: efficiency - but it is small, and most of any single season's overperformance
#: is luck. Carrying the whole residual forward would fade every star; carrying
#: none would ignore real ability. This is the middle, and it is empirical.
RESIDUAL_PERSISTENCE = 0.22


@dataclass
class PriorSeason:
    player_key: str
    games: int
    points_actual: float          # per game
    points_expected: float        # per game
    target_share: float | None
    rush_share: float | None
    snap_pct: float | None

    @property
    def residual(self) -> float:
        return self.points_actual - self.points_expected


@dataclass
class PriorFlag:
    player_key: str
    name: str
    position: str
    games: int
    residual: float
    z: float
    verdict: str                  # inflated | deflated | normal
    reasons: list[str] = field(default_factory=list)

    @property
    def is_flagged(self) -> bool:
        return self.verdict != "normal"

    def describe(self) -> str:
        if self.verdict == "inflated":
            return (
                f"{self.name}: 2025 production ran {self.residual:+.1f} pts/gm above "
                f"his usage ({self.z:+.1f} sd for a {self.position}) — this year's "
                f"price may be anchored to a season he is unlikely to repeat"
            )
        if self.verdict == "deflated":
            return (
                f"{self.name}: 2025 usage implied {abs(self.residual):.1f} pts/gm more "
                f"than he scored ({self.z:+.1f} sd for a {self.position}) — the role "
                f"was better than the box score"
            )
        return f"{self.name}: in line with a typical {self.position}"


def load_prior_season(conn: Database, season: int) -> dict[str, PriorSeason]:
    """Per-player averages from a completed season."""
    rows = conn.fetchall(
        """
        SELECT player_key, COUNT(*) AS games,
               AVG(points_actual)   AS actual,
               AVG(points_expected) AS expected,
               AVG(target_share)    AS target_share,
               AVG(rush_share)      AS rush_share,
               AVG(snap_pct)        AS snap_pct
        FROM player_week_usage
        WHERE season = ? AND points_expected IS NOT NULL
        GROUP BY player_key
        HAVING COUNT(*) >= ?
        """,
        (season, MIN_GAMES),
    )
    return {
        r["player_key"]: PriorSeason(
            player_key=r["player_key"],
            games=int(r["games"]),
            points_actual=float(r["actual"] or 0.0),
            points_expected=float(r["expected"] or 0.0),
            target_share=r["target_share"],
            rush_share=r["rush_share"],
            snap_pct=r["snap_pct"],
        )
        for r in rows
    }


def positional_residuals(
    conn: Database, season: int
) -> dict[str, tuple[float, float]]:
    """position -> (mean residual, sd of residual).

    The baseline the flag is measured against. Without it, every good player
    looks like a regression candidate, because expected points assume average
    efficiency and good players are not average.
    """
    priors = load_prior_season(conn, season)
    if not priors:
        return {}

    positions = {
        r["player_key"]: r["position"]
        for r in conn.fetchall("SELECT player_key, position FROM players")
    }
    grouped: dict[str, list[float]] = {}
    for key, prior in priors.items():
        position = positions.get(key)
        if position:
            grouped.setdefault(position, []).append(prior.residual)

    return {
        position: (fmean(values), pstdev(values) if len(values) > 1 else 1.0)
        for position, values in grouped.items()
        if len(values) >= 5
    }


def flag_players(
    conn: Database, season: int, z_threshold: float = Z_FLAG
) -> dict[str, PriorFlag]:
    """Prior-season regression flags, keyed by player."""
    priors = load_prior_season(conn, season)
    norms = positional_residuals(conn, season)
    meta = {
        r["player_key"]: (r["full_name"], r["position"])
        for r in conn.fetchall("SELECT player_key, full_name, position FROM players")
    }

    out: dict[str, PriorFlag] = {}
    for key, prior in priors.items():
        name, position = meta.get(key, (key, ""))
        norm = norms.get(position)
        if not norm:
            continue
        mean_residual, sd = norm
        if sd <= 0:
            continue

        z = (prior.residual - mean_residual) / sd
        verdict = "normal"
        reasons: list[str] = []

        if z >= z_threshold:
            verdict = "inflated"
            reasons.append(
                f"{prior.residual:+.1f} pts/gm over expected, "
                f"{z:+.1f} sd for a {position}"
            )
        elif z <= -z_threshold:
            verdict = "deflated"
            reasons.append(
                f"{prior.residual:+.1f} pts/gm vs expected, "
                f"{z:+.1f} sd for a {position}"
            )

        out[key] = PriorFlag(
            player_key=key, name=name, position=position, games=prior.games,
            residual=round(prior.residual, 2), z=round(z, 2), verdict=verdict,
            reasons=reasons,
        )
    return out


def measured_volatility(conn: Database, season: int) -> dict[str, float]:
    """Week-to-week coefficient of variation per position, from real games.

    Replaces the hand-set constants in src/projections.py with something
    measured. This directly sets the width of every floor/ceiling band and feeds
    the win-probability model, so guessing it was the weakest link in both.
    """
    rows = conn.fetchall(
        """
        SELECT p.position, u.player_key, u.points_actual
        FROM player_week_usage u JOIN players p USING(player_key)
        WHERE u.season = ? AND u.points_actual IS NOT NULL
        """,
        (season,),
    )
    by_player: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        by_player.setdefault((r["position"], r["player_key"]), []).append(
            float(r["points_actual"])
        )

    # Per-player CV first, then average across players: a single pooled variance
    # would mostly measure the gap between stars and scrubs rather than how much
    # one player swings week to week, which is what a floor/ceiling band needs.
    per_position: dict[str, list[float]] = {}
    for (position, _), values in by_player.items():
        if len(values) < MIN_GAMES:
            continue
        mean = fmean(values)
        if mean <= 1.0:
            continue
        per_position.setdefault(position, []).append(pstdev(values) / mean)

    return {
        position: round(fmean(values), 3)
        for position, values in per_position.items()
        if len(values) >= 5
    }


def expected_carryover(residual: float) -> float:
    """The part of a prior-season residual worth expecting to repeat.

    See RESIDUAL_PERSISTENCE for the measurement behind the coefficient.
    """
    return RESIDUAL_PERSISTENCE * residual


def draft_adjustment(
    projection: float, flag: "PriorFlag | None", strength: float = 1.0
) -> float:
    """Nudge a season projection for prior-year luck.

    Deliberately small, and in the OPPOSITE direction to the residual: a player
    who overperformed is expected to give most of it back. Only the portion that
    does NOT persist is removed, so a genuinely elite player keeps the part of
    his edge that is real.

    `strength` scales the whole adjustment; the projection sources already price
    in some of this, so applying it at full force would double-count.
    """
    if flag is None or not flag.is_flagged:
        return projection
    # Per game -> per season, then remove only the non-repeating share.
    non_persistent = flag.residual * (1.0 - RESIDUAL_PERSISTENCE)
    return projection - strength * non_persistent * flag.games


def usage_priors(conn: Database, season: int) -> dict[str, dict[str, float]]:
    """Prior-year usage rates, for shrinking early-season estimates toward.

    In week 3, a player's own last-season target share is a far better prior
    than the positional average - it already encodes his role, his offence and
    his standing on the depth chart.
    """
    return {
        key: {
            "target_share": prior.target_share or 0.0,
            "rush_share": prior.rush_share or 0.0,
            "snap_pct": prior.snap_pct or 0.0,
            "points": prior.points_actual,
            "points_expected": prior.points_expected,
        }
        for key, prior in load_prior_season(conn, season).items()
    }
