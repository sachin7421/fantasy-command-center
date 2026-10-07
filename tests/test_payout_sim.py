"""Decisions priced in dollars, not playoff percentage points.

The league pays $750/$400/$250 for the top three, $300 for most regular-
season points, $50 for each week's high score (13 of 14 weeks), $50 to the
consolation winner, and fines last place $50 (src/league_bootstrap.PAYOUTS).
A lineup or waiver decision changes several of those at once, so one
simulation plays out the rest of the regular season week by week, seeds both
brackets, and prices each outcome - and conditions on THIS week's result to
say what a win this week is worth.
"""
from __future__ import annotations

import pytest

from src.analytics.payout import simulate_payouts
from src.analytics.season_sim import Matchup, TeamSeason

PAY = {
    "weekly_high_score": 50, "weekly_high_score_weeks": 13, "regular_season_weeks": 14,
    "most_points_regular_season": 300, "consolation_winner": 50,
    "last_place_penalty": 50, "teams": 12,
}
FINISH = {1: 750, 2: 400, 3: 250}


def _league(dominant_mean=160.0, weak_mean=60.0):
    teams = [TeamSeason(str(i), f"T{i}", wins=2, losses=2, points_for=400.0, mean=100.0, sd=15.0)
             for i in range(1, 13)]
    teams[0].mean = dominant_mean      # team 1 crushes everyone
    teams[11].mean = weak_mean         # team 12 loses to everyone
    return teams


def _schedule(weeks=range(5, 15)):
    games = []
    for w in weeks:
        order = list(range(1, 13))
        for i in range(6):
            games.append(Matchup(w, str(order[i]), str(order[11 - i])))
    return games


def test_a_dominant_team_is_priced_at_first_place_plus_most_points_plus_high_scores():
    odds = simulate_payouts(_league(), _schedule(), PAY, FINISH, playoff_spots=6,
                            trials=400, final_week=14)
    top = next(o for o in odds if o.team_key == "1")
    assert top.p_finish[1] > 0.95
    assert top.p_most_points > 0.95
    # Ten weeks left, 13 of 14 paid: at most ten high scores, nearly all his.
    assert 8.5 < top.expected_high_scores <= 10.0
    assert top.expected_dollars > 750 + 300 + 8.5 * 50


def test_a_hopeless_team_is_priced_at_the_last_place_fine():
    odds = simulate_payouts(_league(), _schedule(), PAY, FINISH, playoff_spots=6,
                            trials=400, final_week=14)
    bottom = next(o for o in odds if o.team_key == "12")
    assert bottom.p_last > 0.95
    assert bottom.expected_dollars == pytest.approx(-50, abs=15)


def test_an_unpaid_high_score_week_is_not_counted():
    """13 of 14 weeks pay. With the unpaid week named, that week adds nothing."""
    pay = dict(PAY, unpaid_high_score_week=14)
    with_14 = simulate_payouts(_league(), _schedule(weeks=[14]), pay, FINISH, 6, 300, 14)
    assert next(o for o in with_14 if o.team_key == "1").expected_high_scores == 0.0


def test_this_weeks_win_is_worth_dollars_for_a_bubble_team():
    teams = _league(dominant_mean=100.0, weak_mean=100.0)   # everyone equal
    odds = simulate_payouts(teams, _schedule(), PAY, FINISH, 6, 1500, 14, my_team="6")
    me = next(o for o in odds if o.team_key == "6")
    assert me.next_game is not None
    assert 0.35 < me.next_game.p_win < 0.65
    assert me.next_game.ev_if_win > me.next_game.ev_if_loss
    assert me.next_game.win_value == pytest.approx(
        me.next_game.ev_if_win - me.next_game.ev_if_loss, abs=0.01)
    assert me.next_game.p_high_score == pytest.approx(1 / 12, abs=0.05)


def test_consolation_winner_is_someone_outside_the_playoffs():
    odds = simulate_payouts(_league(), _schedule(), PAY, FINISH, 6, 400, 14)
    top = next(o for o in odds if o.team_key == "1")
    assert top.p_consolation_win == 0.0
    assert sum(o.p_consolation_win for o in odds) == pytest.approx(1.0, abs=0.01)
    assert sum(o.p_finish[1] for o in odds) == pytest.approx(1.0, abs=0.01)
    assert sum(o.p_finish[3] for o in odds) == pytest.approx(1.0, abs=0.01)
