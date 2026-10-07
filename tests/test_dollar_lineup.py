"""The lineup is chosen for dollars: a win is worth what the season says it
is, and the week's high score is worth $50 - so a team with nothing to lose
in its matchup and a live shot at the high score should chase upside, and a
heavy favourite with a lot riding on the win should protect the floor.
"""
from __future__ import annotations

import pytest

from src.analytics.distributions import (
    PlayerForecast, optimise_for_dollars, top_probability,
)

SLOTS = {"QB": 1, "RB": 1, "W/R/T": 1}


def _roster():
    return [
        PlayerForecast("qb", "Steady QB", "QB", "A", 20.0, 4.0),
        PlayerForecast("rb1", "Steady RB", "RB", "B", 14.0, 3.0),
        PlayerForecast("rb2", "Boom RB", "RB", "C", 13.0, 9.0),
        PlayerForecast("wr1", "Steady WR", "WR", "D", 12.0, 3.0),
        PlayerForecast("wr2", "Boom WR", "WR", "E", 11.0, 9.0),
    ]


def test_top_probability_is_one_twelfth_for_identical_teams():
    others = [(100.0, 20.0)] * 11
    assert top_probability(100.0, 20.0, others) == pytest.approx(1 / 12, abs=0.01)


def test_top_probability_rises_with_mean_and_with_variance_when_behind():
    others = [(100.0, 20.0)] * 11
    assert top_probability(120.0, 20.0, others) > top_probability(100.0, 20.0, others)
    # Behind the field, more variance is the only way to the top.
    assert top_probability(85.0, 30.0, others) > top_probability(85.0, 10.0, others)


def test_a_valuable_win_as_a_favourite_protects_the_floor():
    outcome = optimise_for_dollars(
        _roster(), SLOTS, opponent_mean=30.0, opponent_sd=8.0,
        win_value=200.0, high_score_value=50.0, field=[(60.0, 12.0)] * 11,
    )
    chosen = {p.player_key for p in outcome.players}
    assert chosen == {"qb", "rb1", "wr1"}, chosen
    assert outcome.risk <= 0.0
    assert outcome.dollars == pytest.approx(
        outcome.win_probability * 200.0 + outcome.top_probability * 50.0, abs=0.01)


def test_with_nothing_riding_on_the_win_the_high_score_pulls_toward_upside():
    outcome = optimise_for_dollars(
        _roster(), SLOTS, opponent_mean=46.0, opponent_sd=8.0,
        win_value=0.0, high_score_value=50.0, field=[(52.0, 6.0)] * 11,
    )
    chosen = {p.player_key for p in outcome.players}
    assert "rb2" in chosen or "wr2" in chosen, chosen
    assert outcome.risk > 0.0


def test_the_report_says_what_it_is_playing_for():
    outcome = optimise_for_dollars(
        _roster(), SLOTS, opponent_mean=46.0, opponent_sd=8.0,
        win_value=57.0, high_score_value=50.0, field=[(52.0, 6.0)] * 11,
    )
    text = outcome.describe()
    assert "$" in text and "win" in text and "high score" in text


# --- the Thursday/Sunday email says what the week is worth ---------------------

def test_the_lineup_notification_carries_the_money_lines():
    from src.lineup_solver import Lineup
    from src.season import lineup

    report = lineup.LineupReport(
        optimal=Lineup(slots=[], total=0.0, bench=[]), current_points=100.0, optimal_points=103.0,
        swaps=[], risk_mode="ceiling", week=5, roster_size=16, projected=14,
        stakes=["vs Pipelayers: 59% to win, $65 at stake; 7% for the $50 high score."],
    )
    # A posture change alone is worth a mail even with no swaps: the manager
    # should know the model wants him chasing the ceiling this week.
    note = lineup.to_notification(report, 2026)
    assert note is not None
    assert "Pipelayers" in note.text() and "$65" in note.text()
    assert "ceiling" in note.text()
