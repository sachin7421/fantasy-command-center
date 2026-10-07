"""The ground truth comes from the box score, not the opportunity model.

Protocol D, run for real on 7 Oct 2026 the morning Yahoo data access
arrived: 32 of 45 player-weeks on the user's roster matched Yahoo's listed
points to the cent, and every one of the four that differed was a fumble.
`ff_opportunity` has no sack-fumble column and no self-recovered fumbles, so
Jordan Love's two strip-sacks in week 1 (one lost) counted for nothing:
Yahoo 19.48, ours 22.48.

nflverse's weekly `player_stats` is the box score. It carries `fumbles_total`
and `fumbles_lost_total` - which reproduce all four gaps exactly - and the
two return-touchdown categories that had to be declared unfixable before.
"""
from __future__ import annotations

import src.league_bootstrap as bootstrap
from src import scoring
from src.sources.usage import KNOWN_MISSING_STATS, box_score_line

RULES = scoring.build_from_yahoo(bootstrap.build_settings())

#: Jordan Love, 2026 week 1, as nflverse publishes it (the columns we read).
#: Yahoo's listed total for the same game: 19.48.
LOVE_WK1 = {
    "player_id": "00-0036264", "week": 1, "position": "QB",
    "passing_yards": 387, "passing_tds": 2, "passing_interceptions": 1,
    "carries": 3, "rushing_yards": 0, "rushing_tds": 0,
    "receptions": 0, "receiving_yards": 0, "receiving_tds": 0,
    "passing_2pt_conversions": 0, "rushing_2pt_conversions": 0,
    "receiving_2pt_conversions": 0,
    "sack_fumbles": 2, "sack_fumbles_lost": 1,
    "fumbles_total": 2, "fumbles_lost_total": 1,
    "special_teams_tds": 0, "fumble_recovery_tds": 0,
}


def test_loves_week_one_scores_what_yahoo_listed():
    assert round(RULES.score(box_score_line(LOVE_WK1)), 2) == 19.48


def test_a_self_recovered_fumble_costs_one_point_not_zero():
    """Croskey-Merritt wk1: one fumble, recovered by his own team. Yahoo 11.10,
    ours 12.10 - the opportunity dataset only publishes lost ones."""
    row = dict(LOVE_WK1, position="RB", passing_yards=0, passing_tds=0,
               passing_interceptions=0, rushing_yards=121, fumbles_total=1,
               fumbles_lost_total=0, sack_fumbles=0, sack_fumbles_lost=0)
    line = box_score_line(row)
    assert line["fum"] == 1.0 and line["fum_lost"] == 0.0
    assert round(RULES.score(line), 2) == 11.1


def test_return_touchdowns_are_no_longer_a_justified_gap():
    """The box score has special_teams_tds and fumble_recovery_tds."""
    line = box_score_line(dict(LOVE_WK1, special_teams_tds=1, fumble_recovery_tds=1))
    assert line["ret_td"] == 1.0 and line["off_fum_ret_td"] == 1.0
    assert set(RULES.scoring_gaps(line.keys())) == set()
    assert frozenset() == KNOWN_MISSING_STATS


def test_missing_columns_are_zero_not_an_error():
    """A column nflverse drops must not take the whole ingest down."""
    line = box_score_line({"player_id": "x", "week": 2})
    assert all(v == 0.0 for v in line.values())
