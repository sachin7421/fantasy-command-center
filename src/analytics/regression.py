"""Expected points regression: telling luck apart from usage.

    fantasy points = opportunity x efficiency

Opportunity persists; efficiency, and touchdown rate above all, mostly does not.
So a player scoring far above what his usage implies is not "hot" - he is due to
come down, and the market is about to overpay for him. The reverse is the best
buy-low signal available.

The signal:

    residual = actual points - expected points (from usage)

Positive residual = SELL. Negative residual = BUY.

Two guards keep this from being noise:

* The residual is **shrunk** by sample size, so one loud week does not create a
  recommendation (see src/analytics/shrinkage.py).
* A player is only flagged when his **usage itself is stable or rising**. A
  negative residual on a player who is also losing snaps is not bad luck, it is
  a player losing his job - the opposite of a buy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import Any
from collections.abc import Sequence

from src.analytics import shrinkage
from src.storage import Database

#: Standard deviation of the SHRUNK trailing-six-game residual, measured over
#: 12,477 windows (tools/calibrate.py). This is the scale a threshold has to be
#: expressed in.
#:
#: The old constant was 1.8 points per game, justified as "roughly one standard
#: deviation" of the residual. It was one standard deviation of the RAW residual
#: (measured 1.898) while `is_actionable` compared it against the SHRUNK one -
#: two different quantities, a factor of three apart, so 1.8 was really 3.0
#: sigma and the module fired on 0.2%-0.9% of players a season rather than the
#: handful a week its docstring promised. Correcting the shrinkage constant made
#: it worse: at the measured k of 60 an absolute 1.8 fires on exactly nothing.
SHRUNK_RESIDUAL_SD = 0.173

#: How many standard deviations of that distribution count as out of line. Two
#: sigma is about 5% of players, which for a 12-team league scanning a couple of
#: hundred relevant names is the handful a week that was always intended.
FLAG_Z = 2.0
FLAG_THRESHOLD = FLAG_Z * SHRUNK_RESIDUAL_SD

#: Measured coefficient for correcting a projection, from an out-of-sample fit
#: over 8,098 trailing-6 / next-4 windows:
#:
#:     next4_ppg = 1.199 + 0.870 * trailing_actual_ppg - 0.574 * raw_residual
#:
#: So against a points-anchored baseline the right correction is -0.574 of the
#: RAW residual. The old code applied `-strength * shrunk_residual`, which at
#: its default worked out to about -0.167 - the right sign and a third of the
#: right size.
RESIDUAL_CORRECTION = -0.574
#: Minimum games before the signal is trusted at all.
MIN_GAMES = 3
#: Games to look back over. A full season averages the luck away - which is the
#: whole point of regression - so the signal only exists in a recent window.
DEFAULT_WINDOW = 6


@dataclass
class UsageWeek:
    week: int
    points_actual: float
    points_expected: float
    snap_pct: float | None = None
    target_share: float | None = None
    rush_share: float | None = None

    @property
    def residual(self) -> float:
        return self.points_actual - self.points_expected


@dataclass
class RegressionSignal:
    player_key: str
    name: str
    position: str
    team: str
    games: int
    points_actual: float          # per game
    points_expected: float        # per game
    raw_residual: float           # per game, unshrunk
    residual: float               # per game, shrunk by sample size
    usage_trend: float            # change in snap share, recent vs earlier
    verdict: str                  # buy | sell | hold
    confidence: float             # 0-1, the shrinkage weight
    reasons: list[str] = field(default_factory=list)
    #: The threshold this signal was judged against, so a caller that passes a
    #: custom one is not silently re-tested against the module default.
    threshold: float = FLAG_THRESHOLD

    @property
    def is_actionable(self) -> bool:
        return self.verdict in ("buy", "sell") and abs(self.residual) >= self.threshold

    def describe(self) -> str:
        direction = "over" if self.residual > 0 else "under"
        return (
            f"{self.name} ({self.position} {self.team}): "
            f"{self.points_actual:.1f} actual vs {self.points_expected:.1f} expected "
            f"per game - {direction}performing by {abs(self.residual):.1f}"
        )


def usage_trend(weeks: Sequence[UsageWeek]) -> float:
    """Change in snap share between the recent half and the earlier half.

    Distinguishes "unlucky" from "losing his job", which look identical if you
    only read the residual.
    """
    with_snaps = [w for w in weeks if w.snap_pct is not None]
    if len(with_snaps) < 4:
        return 0.0
    midpoint = len(with_snaps) // 2
    earlier = fmean(w.snap_pct for w in with_snaps[:midpoint])
    recent = fmean(w.snap_pct for w in with_snaps[midpoint:])
    return round(recent - earlier, 4)


def analyse(
    player_key: str,
    name: str,
    position: str,
    team: str,
    weeks: Sequence[UsageWeek],
    threshold: float = FLAG_THRESHOLD,
) -> RegressionSignal | None:
    """Turn one player's weekly usage into a buy/sell/hold verdict."""
    played = [w for w in weeks if w.points_expected is not None]
    if len(played) < MIN_GAMES:
        return None

    actual = fmean(w.points_actual for w in played)
    expected = fmean(w.points_expected for w in played)
    raw_residual = actual - expected

    # Shrink toward zero: the prior on luck is that there is none.
    # The residual has its OWN stabilisation constant. Sharing "points" with a
    # player's scoring level meant one number served two estimands whose correct
    # values differ thirty-fold.
    confidence = shrinkage.weight(len(played), metric="points_residual")
    residual = shrinkage.shrink(
        raw_residual, 0.0, len(played), metric="points_residual"
    )
    trend = usage_trend(played)

    reasons: list[str] = []
    verdict = "hold"

    if residual <= -threshold:
        verdict = "buy"
        reasons.append(
            f"scoring {abs(residual):.1f} pts/game below what his usage implies"
        )
        if trend > 0.05:
            reasons.append(f"snap share also rising ({trend:+.0%})")
        elif trend < -0.10:
            # The residual is real but the cause is bad, not unlucky.
            verdict = "hold"
            reasons = [
                f"underperforming, but snap share is falling ({trend:+.0%}) - "
                "this looks like a shrinking role, not bad luck"
            ]
    elif residual >= threshold:
        verdict = "sell"
        reasons.append(
            f"scoring {residual:.1f} pts/game above what his usage implies"
        )
        if trend < -0.05:
            reasons.append(f"snap share also falling ({trend:+.0%})")

    # `confidence` is the shrinkage weight, and for a residual that is nearly
    # all noise it is structurally low - it was 0.33 for EVERY signal at the old
    # constant and is about 0.09 at the measured one. Repeating "treat as a
    # lean" on every line taught the reader to ignore the caveat, so it is only
    # said when the SAMPLE is genuinely short rather than when the statistic is
    # simply a noisy one.
    if len(played) < DEFAULT_WINDOW and verdict != "hold":
        reasons.append(
            f"only {len(played)} games so far - thinner than the usual "
            f"{DEFAULT_WINDOW}-game window"
        )

    return RegressionSignal(
        player_key=player_key, name=name, position=position, team=team,
        games=len(played), points_actual=round(actual, 2),
        points_expected=round(expected, 2), raw_residual=round(raw_residual, 2),
        residual=round(residual, 2), usage_trend=trend, verdict=verdict,
        confidence=round(confidence, 3), reasons=reasons, threshold=threshold,
    )


def load_weeks(conn: Database, season: int, through_week: int | None = None) -> dict:
    """Weekly usage per player, oldest first."""
    sql = (
        "SELECT u.player_key, u.week, u.points_actual, u.points_expected, "
        "       u.snap_pct, u.target_share, u.rush_share, "
        "       p.full_name, p.position, p.team "
        "FROM player_week_usage u JOIN players p USING(player_key) "
        "WHERE u.season=? AND u.points_expected IS NOT NULL"
    )
    params: list[Any] = [season]
    if through_week:
        sql += " AND u.week<=?"
        params.append(through_week)
    sql += " ORDER BY u.player_key, u.week"

    grouped: dict[str, dict[str, Any]] = {}
    for row in conn.fetchall(sql, tuple(params)):
        entry = grouped.setdefault(
            row["player_key"],
            {
                "name": row["full_name"], "position": row["position"],
                "team": row["team"] or "", "weeks": [],
            },
        )
        entry["weeks"].append(
            UsageWeek(
                week=int(row["week"]),
                points_actual=float(row["points_actual"] or 0.0),
                points_expected=float(row["points_expected"] or 0.0),
                snap_pct=row["snap_pct"],
                target_share=row["target_share"],
                rush_share=row["rush_share"],
            )
        )
    return grouped


def scan(
    conn: Database,
    season: int,
    through_week: int | None = None,
    threshold: float = FLAG_THRESHOLD,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    window: int = DEFAULT_WINDOW,
) -> list[RegressionSignal]:
    """Every actionable buy/sell signal in the league, strongest first.

    `window` is the trailing number of games considered. Scanning a whole
    season finds almost nothing, correctly: luck cancels out over seventeen
    games. The tradeable signal is in the recent stretch, before it has.
    """
    signals = []
    for player_key, entry in load_weeks(conn, season, through_week).items():
        if entry["position"] not in positions:
            continue
        weeks = entry["weeks"][-window:] if window else entry["weeks"]
        signal = analyse(
            player_key, entry["name"], entry["position"], entry["team"],
            weeks, threshold,
        )
        if signal and signal.is_actionable:
            signals.append(signal)
    signals.sort(key=lambda s: abs(s.residual), reverse=True)
    return signals


def adjusted_projection(
    baseline: float, signal: RegressionSignal | None, strength: float = 1.0
) -> float:
    """Correct a per-game projection toward what usage implies.

    Uses the measured coefficient on the RAW residual rather than an assumed
    fraction of the shrunk one. Fitted out-of-sample over 8,098 windows:

        next4_ppg = 1.199 + 0.870 * trailing_actual - 0.574 * raw_residual

    `strength` scales it, and defaults to 1.0 now that the coefficient itself
    is measured. Reduce it if the projection being corrected has already priced
    some of the effect in - a blended pre-season number has, a naive trailing
    average has not.

    `baseline` is points PER GAME, which is the scale the coefficient was fitted
    on.
    """
    if signal is None or signal.verdict == "hold":
        return baseline
    return baseline + strength * RESIDUAL_CORRECTION * signal.raw_residual
