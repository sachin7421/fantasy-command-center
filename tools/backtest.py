"""Measure projection error against what actually happened.

Every performance claim this project has made was circular: the mock draft and
the 36-draft simulation both scored our team AND its opponents with our own
projections, which measures self-consistency and not edge. This does not do
that. It compares a projection made BEFORE a week to the points actually
scored in it.

What it is for, beyond honesty. `distributions.py` needs the spread of a
player's outcome around his projection - Var(actual | projection) - and what
it currently uses is `VOLATILITY_FIT`, a fit of realized season standard
deviation against realized season mean. That is a different quantity: it
describes dispersion around a mean you do not know at decision time, and
carries no projection error at all. Every win probability built on it is
therefore overconfident, and the "when behind, buy variance" logic under-fires.

This measures the thing directly.

    python tools/backtest.py              # all positions
    python tools/backtest.py --position RB

Reads only. Writes nothing, changes no constant - it prints what the data says
so a person can decide whether to move one.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db
from src.config import Config

QUERY = """
SELECT p.position,
       h.player_key,
       h.week,
       h.points AS projected,
       a.points AS actual
FROM projection_history h
JOIN player_week_actuals a
  ON a.player_key = h.player_key AND a.season = h.season AND a.week = h.week
JOIN players p ON p.player_key = h.player_key
WHERE h.season = ?
  AND h.points IS NOT NULL
  AND a.points IS NOT NULL
"""


def summarise(rows) -> dict[str, dict[str, float]]:
    """Error statistics per position, and overall."""
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        position = str(row["position"] or "?").upper()
        pair = (float(row["projected"]), float(row["actual"]))
        buckets[position].append(pair)
        buckets["ALL"].append(pair)

    out: dict[str, dict[str, float]] = {}
    for position, pairs in sorted(buckets.items()):
        if len(pairs) < 30:
            continue
        errors = [actual - projected for projected, actual in pairs]
        projected = [p for p, _ in pairs]
        actual = [a for _, a in pairs]

        out[position] = {
            "n": len(pairs),
            "mean_projected": statistics.fmean(projected),
            "mean_actual": statistics.fmean(actual),
            # Bias: positive means the projections run LOW.
            "bias": statistics.fmean(errors),
            "mae": statistics.fmean([abs(e) for e in errors]),
            "rmse": math.sqrt(statistics.fmean([e * e for e in errors])),
            # The quantity distributions.py actually needs.
            "sd_error": statistics.pstdev(errors),
            "correlation": _correlation(projected, actual),
        }
    return out


def _correlation(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else 0.0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--position")
    parser.add_argument("--db")
    args = parser.parse_args(argv)

    cfg = Config.load("config.yaml")
    conn = db.init_db(args.db or cfg.db_path)
    try:
        rows = conn.fetchall(QUERY, (args.season,))
    finally:
        conn.close()

    if not rows:
        print(f"No paired projections and results for {args.season}.")
        print("This needs projection_history AND player_week_actuals for the")
        print("same player-weeks. `fcc sync` fills the first as it goes;")
        print("`fcc sync-usage` fills the second.")
        return 1

    stats = summarise(rows)
    if args.position:
        stats = {k: v for k, v in stats.items() if k == args.position.upper()}

    print(f"Projection error, season {args.season} - out of sample.\n")
    print(f"  {'pos':<5} {'n':>6} {'proj':>7} {'actual':>7} {'bias':>7} "
          f"{'MAE':>7} {'RMSE':>7} {'sd(err)':>8} {'r':>6}")
    for position, s in stats.items():
        print(
            f"  {position:<5} {s['n']:>6.0f} {s['mean_projected']:>7.2f} "
            f"{s['mean_actual']:>7.2f} {s['bias']:>+7.2f} {s['mae']:>7.2f} "
            f"{s['rmse']:>7.2f} {s['sd_error']:>8.2f} {s['correlation']:>6.2f}"
        )

    print("\nWhat these mean:")
    print("  bias     positive = the projections run LOW")
    print("  sd(err)  the spread the lineup model needs, and does not use -")
    print("           it currently fits dispersion around a realized season")
    print("           mean, which carries no projection error at all")
    print("  r        correlation between projection and outcome. Weekly")
    print("           fantasy scoring is extremely noisy; do not expect much")

    overall = stats.get("ALL")
    if overall and overall["correlation"] < 0.1:
        print("\n  WARNING: essentially no relationship between projection and")
        print("  outcome in this sample. Check the join before believing any")
        print("  of the above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
