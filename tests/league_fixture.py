"""One deterministic league, shared by the invariant and golden suites.

Both of those suites answer questions about NUMBERS, not about control flow, so
they need a board that is fixed forever: same players, same projections, same
ADP, every run on every machine. Nothing here reads a file, a clock, or a random
seed, because a golden test whose input can drift is a golden test that fails
for reasons nobody can act on.

The shape is deliberately awkward in the places real drafts are awkward - a
steep top of the running back board, a flat middle at receiver, one tight end
worth more than the rest, quarterbacks who score more in absolute terms while
being worth less - so that a change which flattens the model shows up here
rather than in September.
"""
from __future__ import annotations

from pathlib import Path

from src import db, scoring
from src.idmap import IdMapper
from src.storage import Database

SLOTS = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 2, "DEF": 1}
NUM_TEAMS = 12
SEASON = 2026

TEAMS = ["BUF", "MIA", "NYJ", "NE", "BAL", "CIN", "CLE", "PIT",
         "HOU", "IND", "JAX", "TEN"]


def _projection(position: str, rank: int) -> float:
    """Points for the nth best player at a position.

    Each curve is a different SHAPE, not the same line at a different height.
    That matters: replacement level, tier breaks and positional scarcity are all
    statements about curvature, and a fixture built from parallel lines would
    let a broken scarcity model pass.
    """
    if position == "RB":
        # Steep cliff early, then a long flat tail - the classic scarcity case.
        return 320.0 - 14.0 * rank ** 0.72
    if position == "WR":
        # Shallow and deep: many receivers are nearly interchangeable.
        return 300.0 - 4.2 * rank
    if position == "QB":
        # Highest raw totals in the league, and almost no spread. A model that
        # ranks on points alone puts these first; a correct one does not.
        return 380.0 - 3.1 * rank
    if position == "TE":
        # One outlier, then a cliff, then nothing. The hardest positional shape.
        return 250.0 - 40.0 * rank ** 0.55
    return 140.0 - 2.0 * rank


def _adp(position: str, rank: int) -> float:
    """Where the market takes him - deliberately NOT the same order as points.

    The gap between these two curves is the entire edge the board claims to
    have. If they agreed, VORP would be a re-sort of ADP and worth nothing.
    """
    base = {"RB": 1.0, "WR": 3.0, "QB": 30.0, "TE": 22.0, "DEF": 130.0}[position]
    slope = {"RB": 2.6, "WR": 2.4, "QB": 4.5, "TE": 6.0, "DEF": 1.0}[position]
    return base + slope * rank


COUNTS = {"RB": 60, "WR": 70, "QB": 24, "TE": 24, "DEF": 12}


def build_league(path: Path) -> tuple[Database, object]:
    """A full board written to a fresh SQLite file at `path`."""
    from tests.conftest import YAHOO_SETTINGS_FOR_FIXTURE

    conn = db.init_db(path)
    idmap = IdMapper(conn)
    rules = scoring.build_from_yahoo(YAHOO_SETTINGS_FOR_FIXTURE)

    now = "2026-08-25T00:00:00+00:00"   # frozen: a golden file cannot hold a clock
    for position, count in COUNTS.items():
        for rank in range(1, count + 1):
            name = f"{position}{rank}"
            team = TEAMS[(rank - 1) % len(TEAMS)] if position == "DEF" else "AAA"
            key = idmap.upsert_player(full_name=name, position=position, team=team)
            conn.execute(
                "INSERT INTO projections(player_key, source, season, week, "
                "stats_json, points, fetched_at) VALUES (?,?,?,?,?,?,?)",
                (key, "sleeper", SEASON, 0, "{}",
                 round(_projection(position, rank), 4), now),
            )
            ecr = _adp(position, rank)
            conn.execute(
                "INSERT INTO adp(player_key, source, adp, stdev, best, worst, "
                "fetched_at) VALUES (?,?,?,?,?,?,?)",
                (key, "fantasypros", round(ecr, 4), 8.0, ecr - 12, ecr + 12, now),
            )
    conn.commit()
    return conn, rules
