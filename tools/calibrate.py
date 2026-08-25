"""Measure the constants the models depend on, instead of asserting them.

Run:  python tools/calibrate.py --db <a database with player_week_usage>

Several constants in this project were documented as "measured" or "calibrated"
with no artifact that produced them, and a review found most of them wrong by
2-8x. Some were the right idea in the wrong units: the empirical-Bayes
stabilisation points were literature values quoted in *opportunities* (targets,
carries) and used as *games*. Others had the wrong sign.

So the numbers now come from here. This prints a block that can be pasted into
the modules, and `tests/test_calibration.py` asserts the shipped constants still
match what the data says, within a tolerance - so drift is caught rather than
discovered.

Every figure is computed from `player_week_usage`, which `fcc sync-usage`
populates from nflverse. Four seasons is about 22,000 player-weeks.
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POSITIONS = ("QB", "RB", "WR", "TE")
#: A player has to be plausibly startable before his week-to-week behaviour
#: tells us anything about the players we actually make decisions on.
STARTABLE_PPG = 8.0
MIN_GAMES = 6


def _rows(conn, seasons=None):
    sql = (
        "SELECT u.player_key, u.season, u.week, u.team, p.position, "
        "       u.points_actual, u.points_expected, u.snap_pct, "
        "       u.target_share, u.rush_share "
        "FROM player_week_usage u JOIN players p USING(player_key) "
        "WHERE u.points_actual IS NOT NULL AND p.position IN ('QB','RB','WR','TE')"
    )
    params: list = []
    if seasons:
        sql += " AND u.season IN (" + ",".join("?" for _ in seasons) + ")"
        params = list(seasons)
    return conn.fetchall(sql, params)


def _by_player_season(rows):
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r["player_key"], r["season"], r["position"])].append(r)
    return grouped


# --- weekly volatility -------------------------------------------------------

def weekly_volatility(rows) -> dict[str, float]:
    """Within-player coefficient of variation, week to week."""
    per_pos = defaultdict(list)
    for (_, _, pos), weeks in _by_player_season(rows).items():
        points = [float(w["points_actual"] or 0) for w in weeks]
        if len(points) < MIN_GAMES:
            continue
        mean = statistics.fmean(points)
        if mean < STARTABLE_PPG:
            continue
        per_pos[pos].append(statistics.stdev(points) / mean)
    return {pos: round(statistics.fmean(v), 3) for pos, v in sorted(per_pos.items())}


def volatility_slope(rows) -> dict[str, tuple[float, float]]:
    """Fit sd = a + b*mean per position.

    A constant CV forces the standard deviation through the origin. The real
    relationship has a large positive intercept, so a constant CV overstates
    the spread of high scorers and understates it for low ones - which is
    exactly backwards for a start/sit decision that ranks on mean + risk*sd.
    """
    out = {}
    per_pos = defaultdict(list)
    for (_, _, pos), weeks in _by_player_season(rows).items():
        points = [float(w["points_actual"] or 0) for w in weeks]
        if len(points) < MIN_GAMES:
            continue
        per_pos[pos].append((statistics.fmean(points), statistics.stdev(points)))
    for pos, pairs in sorted(per_pos.items()):
        n = len(pairs)
        if n < 30:
            continue
        mx = statistics.fmean(m for m, _ in pairs)
        my = statistics.fmean(s for _, s in pairs)
        denom = sum((m - mx) ** 2 for m, _ in pairs)
        slope = sum((m - mx) * (s - my) for m, s in pairs) / denom if denom else 0.0
        out[pos] = (round(my - slope * mx, 3), round(slope, 3))
    return out


def games_played(rows) -> dict[str, tuple[float, float]]:
    """Mean and sd of games played, among fantasy-relevant players.

    Season-total uncertainty was modelled as weekly_cv / sqrt(17), which assumes
    every player plays every week. Quarterbacks average twelve. The games-played
    term alone is larger than the entire modelled season spread.
    """
    per_pos = defaultdict(list)
    for (_, _, pos), weeks in _by_player_season(rows).items():
        points = [float(w["points_actual"] or 0) for w in weeks]
        if statistics.fmean(points) < 6.0:
            continue
        per_pos[pos].append(len(weeks))
    return {
        pos: (round(statistics.fmean(v), 2),
              round(statistics.stdev(v), 2) if len(v) > 1 else 0.0)
        for pos, v in sorted(per_pos.items())
    }


def season_cv(rows) -> dict[str, float]:
    """Total spread of a season projection: games played AND points per game.

    CV_season^2 = CV_games^2 + CV_ppg^2, treating the two as independent, which
    understates it slightly (injuries lower both).
    """
    games = games_played(rows)
    weekly = weekly_volatility(rows)
    out = {}
    for pos in POSITIONS:
        if pos not in games or pos not in weekly:
            continue
        mean_g, sd_g = games[pos]
        cv_g = sd_g / mean_g if mean_g else 0.0
        # The per-game mean is itself estimated from those games.
        cv_ppg = weekly[pos] / math.sqrt(mean_g) if mean_g else 0.0
        out[pos] = round(math.sqrt(cv_g ** 2 + cv_ppg ** 2), 3)
    return out


# --- correlation -------------------------------------------------------------

def _z_scored(rows):
    """Deviation from each player's own season mean, in his own sd units.

    Removes the player level, so what is left is the week-to-week co-movement
    that a lineup's joint distribution actually depends on.
    """
    out = defaultdict(dict)
    for (key, season, pos), weeks in _by_player_season(rows).items():
        points = [float(w["points_actual"] or 0) for w in weeks]
        if len(points) < MIN_GAMES or statistics.fmean(points) < STARTABLE_PPG:
            continue
        mean = statistics.fmean(points)
        sd = statistics.stdev(points)
        if sd <= 0:
            continue
        for w in weeks:
            team = (w["team"] or "").upper()
            if not team:
                continue
            out[(season, int(w["week"]), team)][key] = (
                pos, (float(w["points_actual"] or 0) - mean) / sd
            )
    return out


def _corr(pairs) -> tuple[float, float, int]:
    n = len(pairs)
    if n < 30:
        return (0.0, 0.0, n)
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    r = num / (dx * dy) if dx and dy else 0.0
    return (round(r, 3), round((1 - r * r) / math.sqrt(n - 1), 3), n)


def correlations(rows) -> dict[str, tuple[float, float, int]]:
    """Same-team, same-week co-movement by position pair."""
    buckets: dict[str, list] = defaultdict(list)
    for players in _z_scored(rows).values():
        items = list(players.items())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (_, (pos_a, za)), (_, (pos_b, zb)) = items[i], items[j]
                a, b = sorted((pos_a, pos_b))
                if a == "QB" and b in ("WR", "TE"):
                    label = "qb_passcatcher"
                elif a == b:
                    label = f"same_position_{a.lower()}"
                else:
                    label = f"{a.lower()}_{b.lower()}"
                buckets[label].append((za, zb))
    return {k: _corr(v) for k, v in sorted(buckets.items()) if len(v) >= 30}


# --- prior-season persistence ------------------------------------------------

def residual_persistence(conn, rows) -> dict[str, float]:
    """Regression slope of this season's points-over-expected on last season's.

    The slope, not the correlation: the coefficient a shrinkage step needs is
    beta, and quoting r overstates it whenever the two seasons have different
    spreads. Reported per position, because it varies by a factor of two.
    """
    per_player = {}
    for (key, season, pos), weeks in _by_player_season(rows).items():
        usable = [
            w for w in weeks
            if w["points_expected"] is not None and w["points_actual"] is not None
        ]
        if len(usable) < 8:
            continue
        per_player[(key, season)] = (
            pos,
            statistics.fmean(
                float(w["points_actual"]) - float(w["points_expected"]) for w in usable
            ),
        )

    per_pos = defaultdict(list)
    for (key, season), (pos, resid) in per_player.items():
        nxt = per_player.get((key, season + 1))
        if nxt and nxt[0] == pos:
            per_pos[pos].append((resid, nxt[1]))

    out = {}
    for pos, pairs in sorted(per_pos.items()):
        if len(pairs) < 40:
            continue
        mx = statistics.fmean(a for a, _ in pairs)
        my = statistics.fmean(b for _, b in pairs)
        denom = sum((a - mx) ** 2 for a, _ in pairs)
        out[pos] = round(
            sum((a - mx) * (b - my) for a, b in pairs) / denom if denom else 0.0, 3
        )
    return out


def positional_residual_bias(rows) -> dict[str, float]:
    """Mean points-over-expected per position.

    Not luck: a structural offset in the upstream expected-points model that is
    the same sign every season. Subtracting a raw residual therefore removes a
    level that was never noise - tight ends run +0.2 to +0.4 every year.
    """
    per_pos = defaultdict(list)
    for r in rows:
        if r["points_expected"] is None:
            continue
        per_pos[r["position"]].append(
            float(r["points_actual"] or 0) - float(r["points_expected"])
        )
    return {
        pos: round(statistics.fmean(v), 3)
        for pos, v in sorted(per_pos.items()) if len(v) >= 100
    }


# --- bust mass ---------------------------------------------------------------

def bust_probability(rows) -> dict[str, float]:
    """P(a startable player scores under 2 points).

    A gamma marginal puts 2-6x too little mass here. The dud game - early exit,
    blowout game script - is most of what a floor is meant to describe.
    """
    per_pos = defaultdict(lambda: [0, 0])
    for (_, _, pos), weeks in _by_player_season(rows).items():
        points = [float(w["points_actual"] or 0) for w in weeks]
        if len(points) < MIN_GAMES or statistics.fmean(points) < STARTABLE_PPG:
            continue
        for p in points:
            per_pos[pos][1] += 1
            if p < 2.0:
                per_pos[pos][0] += 1
    return {
        pos: round(bad / total, 4)
        for pos, (bad, total) in sorted(per_pos.items()) if total
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite file with player_week_usage")
    parser.add_argument("--seasons", help="comma-separated, e.g. 2022,2023,2024")
    args = parser.parse_args()

    os.environ.pop("DATABASE_URL", None)
    from src import db, storage

    storage.database_url = lambda: None
    conn = db.init_db(args.db, force_sqlite=True)

    seasons = [int(s) for s in args.seasons.split(",")] if args.seasons else None
    rows = _rows(conn, seasons)
    if not rows:
        print("No usage rows. Run: fcc sync-usage --season <year>", file=sys.stderr)
        return 2

    span = sorted({r["season"] for r in rows})
    print(f"{len(rows):,} player-weeks, seasons {span[0]}-{span[-1]}\n")

    print("POSITION_VOLATILITY  (weekly CV, src/projections.py)")
    for pos, cv in weekly_volatility(rows).items():
        print(f"    {pos:<4} {cv}")

    print("\nVOLATILITY_FIT  sd = a + b*mean  (replaces constant CV)")
    for pos, (a, b) in volatility_slope(rows).items():
        print(f"    {pos:<4} a={a:>7}  b={b}")

    print("\nGAMES_PLAYED  (mean, sd)")
    for pos, (m, s) in games_played(rows).items():
        print(f"    {pos:<4} {m:>6} +/- {s}")

    print("\nSEASON_CV  (games-played AND per-game uncertainty)")
    for pos, cv in season_cv(rows).items():
        print(f"    {pos:<4} {cv}")

    print("\nCORRELATIONS  (r, standard error, n)")
    for label, (r, se, n) in correlations(rows).items():
        print(f"    {label:<22} {r:>7} +/- {se:<6} n={n:,}")

    print("\nRESIDUAL_PERSISTENCE  (regression slope, season to season)")
    for pos, beta in residual_persistence(conn, rows).items():
        print(f"    {pos:<4} {beta}")

    print("\nPOSITIONAL_RESIDUAL_BIAS  (structural, not luck)")
    for pos, bias in positional_residual_bias(rows).items():
        print(f"    {pos:<4} {bias:+}")

    print("\nBUST_PROBABILITY  P(points < 2 | startable)")
    for pos, p in bust_probability(rows).items():
        print(f"    {pos:<4} {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
