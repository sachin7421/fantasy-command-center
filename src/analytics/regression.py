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
from typing import Any, Sequence

from src.analytics import shrinkage
from src.storage import Database

#: Residual per game (in league points) beyond which a player is worth flagging.
#: Calibrated against 2025: over a trailing six-game window the residual has a
#: standard deviation of about 1.7 points per game, so this is roughly one
#: standard deviation - uncommon enough to mean something, common enough to
#: surface a handful of names a week.
FLAG_THRESHOLD = 1.8
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
    confidence = shrinkage.weight(len(played), metric="points")
    residual = shrinkage.shrink(raw_residual, 0.0, len(played), metric="points")
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

    if confidence < 0.45 and verdict != "hold":
        reasons.append(f"only {len(played)} games - treat as a lean, not a call")

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
    baseline: float, signal: RegressionSignal | None, strength: float = 0.5
) -> float:
    """Nudge a projection toward what usage implies.

    Deliberately partial: the projection sources already price in some of this,
    so applying the whole residual would double-count. `strength` is how much of
    the shrunk residual to remove.
    """
    if signal is None or signal.verdict == "hold":
        return baseline
    return baseline - strength * signal.residual
