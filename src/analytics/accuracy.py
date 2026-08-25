"""Which projection source is actually best - for THIS league's scoring?

Every source is graded against what happened, in league points, and the blend
weights are then earned rather than assumed. My starting 50/50 between Sleeper
and ESPN was a guess; after a few weeks it does not have to be.

This is the part no commercial tool will do for you, because no vendor is going
to tell you a competitor projects your league better than they do.

Three numbers per source, per position:

* **MAE** - typical miss, in points. The headline.
* **RMSE** - punishes large misses, so it catches a source that is usually fine
  but occasionally wild.
* **Bias** - systematic over- or under-projection. A source that is reliably
  10% high is still useful once you know to discount it, which MAE alone hides.

Weights are derived from inverse MAE and then shrunk toward equal weighting,
because early in a season the accuracy estimates are themselves small samples -
the same discipline applied to players in src/analytics/shrinkage.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from collections.abc import Iterable

from src import db
from src.analytics import shrinkage
from src.storage import Database

#: Games of evidence at which measured accuracy is trusted over equal weighting.
WEIGHT_STABILISATION = 40.0
#: Minimum paired observations before a source/position is scored at all.
MIN_OBSERVATIONS = 8


@dataclass
class Accuracy:
    source: str
    position: str
    n: int
    mae: float
    rmse: float
    bias: float

    def describe(self) -> str:
        lean = (
            f"runs {abs(self.bias):.1f} pts {'high' if self.bias > 0 else 'low'}"
            if abs(self.bias) >= 0.5 else "no systematic lean"
        )
        return (
            f"{self.source:<12} {self.position:<4} n={self.n:<5} "
            f"MAE {self.mae:5.2f}  RMSE {self.rmse:5.2f}  {lean}"
        )


def score_sources(
    conn: Database,
    season: int,
    through_week: int | None = None,
    positions: Iterable[str] = ("QB", "RB", "WR", "TE", "DEF"),
) -> list[Accuracy]:
    """Compare every stored weekly projection against the actual result."""
    sql = """
        SELECT j.source, p.position,
               j.points AS projected,
               a.points AS actual
        FROM projections j
        JOIN player_week_actuals a
          ON a.player_key = j.player_key
         AND a.season = j.season
         AND a.week = j.week
        JOIN players p ON p.player_key = j.player_key
        WHERE j.season = ? AND j.week > 0
          AND j.points IS NOT NULL AND a.points IS NOT NULL
    """
    params: list[Any] = [season]
    if through_week:
        sql += " AND j.week <= ?"
        params.append(through_week)

    buckets: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in conn.fetchall(sql, tuple(params)):
        if row["position"] not in positions:
            continue
        buckets.setdefault((row["source"], row["position"]), []).append(
            (float(row["projected"]), float(row["actual"]))
        )

    out: list[Accuracy] = []
    for (source, position), pairs in buckets.items():
        if len(pairs) < MIN_OBSERVATIONS:
            continue
        errors = [projected - actual for projected, actual in pairs]
        n = len(errors)
        out.append(
            Accuracy(
                source=source,
                position=position,
                n=n,
                mae=round(sum(abs(e) for e in errors) / n, 3),
                rmse=round(math.sqrt(sum(e * e for e in errors) / n), 3),
                bias=round(sum(errors) / n, 3),
            )
        )
    out.sort(key=lambda a: (a.position, a.mae))
    return out


def store(conn: Database, results: Iterable[Accuracy], season: int, week: int) -> int:
    stored = 0
    computed_at = db.utcnow()
    for r in results:
        conn.execute(
            "INSERT INTO source_accuracy(source, season, week, position, n, mae, "
            "rmse, bias, computed_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source, season, week, position) DO UPDATE SET "
            "n=excluded.n, mae=excluded.mae, rmse=excluded.rmse, "
            "bias=excluded.bias, computed_at=excluded.computed_at",
            (r.source, season, week, r.position, r.n, r.mae, r.rmse, r.bias, computed_at),
        )
        stored += 1
    conn.commit()
    return stored


#: For a roughly normal error distribution, sd = MAE * sqrt(pi/2). Only the
#: mean absolute error is stored, and the optimal combination rule needs the
#: variance, so this converts between them.
MAE_TO_SD = 1.2533


def derive_weights(
    results: Iterable[Accuracy], position: str | None = None
) -> dict[str, float]:
    """Blend weights earned from measured accuracy.

    Weight is proportional to 1/MSE after removing each source's measured bias,
    then shrunk toward equal weighting by how much INDEPENDENT evidence there
    is. Three corrections over the first version:

    * **1/MSE, not 1/MAE.** For combining forecasts the optimal weight is the
      inverse error VARIANCE. Simulated on three sources with error standard
      deviations of 4/5/6.5 plus a shared component, 1/MAE weights the best
      source at 0.391 where the optimum is 0.490 - a fifth too little.
    * **A source's bias is subtracted rather than merely reported.** The module
      docstring argues correctly that a source running 10% high is useful once
      discounted, and then nothing discounted it. Removing a measured bias is a
      free reduction in mean squared error of exactly bias squared.
    * **`n` counts player-weeks, not source-observations.** It was summed
      across sources, so three sources scoring the same 300 player-weeks
      reported 900 and the shrinkage guard - which exists to stop a two-week
      lead swinging the blend - was already at 0.96 after two weeks.
    """
    relevant = [r for r in results if position is None or r.position == position]
    if not relevant:
        return {}

    by_source: dict[str, list[Accuracy]] = {}
    for r in relevant:
        by_source.setdefault(r.source, []).append(r)
    if len(by_source) < 2:
        return dict.fromkeys(by_source, 1.0)

    # Mean squared error, weighted by each measurement's own sample size, with
    # the source's bias removed first.
    mse: dict[str, float] = {}
    for source, rows in by_source.items():
        weight_total = sum(r.n for r in rows) or 1
        # MAE is what is stored; for roughly normal errors sd ~ MAE * 1.2533.
        variance = sum(
            r.n * ((r.mae * MAE_TO_SD) ** 2 - (r.bias or 0.0) ** 2)
            for r in rows
        ) / weight_total
        mse[source] = max(variance, 1e-6)

    raw = {s: 1.0 / v for s, v in mse.items()}
    total_raw = sum(raw.values()) or 1.0
    measured = {s: v / total_raw for s, v in raw.items()}

    # Independent evidence: the distinct player-weeks scored, which is the
    # LARGEST any single source saw, not the sum over sources.
    n = max(
        (sum(r.n for r in rows) for rows in by_source.values()), default=0
    )

    equal = 1.0 / len(measured)
    w = shrinkage.weight(n, k=WEIGHT_STABILISATION)
    return {
        source: round(w * value + (1.0 - w) * equal, 4)
        for source, value in measured.items()
    }


def report(results: list[Accuracy]) -> list[str]:
    """Human-readable summary, best source per position first."""
    if not results:
        return [
            "  Not enough paired projections and results yet.",
            "  This becomes meaningful after a few weeks of the season.",
        ]

    lines: list[str] = []
    by_position: dict[str, list[Accuracy]] = {}
    for r in results:
        by_position.setdefault(r.position, []).append(r)

    for position in sorted(by_position):
        ranked = sorted(by_position[position], key=lambda a: a.mae)
        lines.append(f"__{position}__")
        for r in ranked:
            lines.append(f"  {r.describe()}")
        if len(ranked) > 1:
            best, worst = ranked[0], ranked[-1]
            gap = worst.mae - best.mae
            if gap >= 0.25:
                lines.append(
                    f"  -> {best.source} is better by {gap:.2f} pts/game at {position}"
                )
        lines.append("")
    return lines
