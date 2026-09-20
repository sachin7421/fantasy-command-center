"""This league's own scoring rules, scored through the settings actually in use.

METHOD.md 4 found the hole: zeroing the league's interception override in
`src/league_bootstrap.py` left tests/test_scoring.py and tests/test_golden.py
entirely green. Both build their rules from a fixture in conftest, which is the
right way to test the ENGINE - and means nothing at all about the rules this
league is scored by. `build_settings()` is the live configuration (there is no
scoring section in config.yaml), so a hand edit to it changed every number the
app produces with nothing to say so.

CLAUDE.md calls the scoring engine the foundation. These are its two documented
overrides and its half-PPR reception value, asserted as behaviour: what a stat
line is worth, not what a constant says.
"""
from __future__ import annotations

import pytest

from src import league_bootstrap, scoring
from src.sources.sleeper_projections import STAT_MAP


@pytest.fixture
def rules():
    return scoring.build_from_yahoo(league_bootstrap.build_settings())


def test_an_interception_costs_a_point(rules):
    """A league override: Yahoo's default is -1 but leagues change it, and this
    one keeps it. Scored, not read off the table."""
    assert rules.score({"pass_int": 1.0}) == pytest.approx(-1.0)


def test_a_fumble_costs_a_point_and_losing_it_costs_another(rules):
    """The second override, and the subtle one: this league scores BOTH
    `Fumbles` and `Fumbles Lost`, so a lost fumble is -2, not -1."""
    assert rules.score({"fum": 1.0}) == pytest.approx(-1.0)
    assert rules.score({"fum": 1.0, "fum_lost": 1.0}) == pytest.approx(-2.0)


def test_a_reception_is_worth_half_a_point(rules):
    assert rules.score({"rec": 1.0}) == pytest.approx(0.5)
    assert rules.ppr_value == pytest.approx(0.5)


def test_a_two_point_conversion_is_worth_two(rules):
    assert rules.score({STAT_MAP["pass_2pt"]: 1.0}) == pytest.approx(2.0)


def test_a_passing_touchdown_is_worth_four(rules):
    assert rules.score({"pass_td": 1.0}) == pytest.approx(4.0)


def test_a_full_line_scores_the_sum_of_its_parts(rules):
    """One realistic quarterback week, hand-computed from the league's own
    rules: 300 passing yards (12) + 2 TD (8) - 1 INT (-1) + 30 rushing yards
    (3) - 1 fumble lost (-2) = 20.0
    """
    line = {
        "pass_yds": 300.0, "pass_td": 2.0, "pass_int": 1.0,
        "rush_yds": 30.0, "fum": 1.0, "fum_lost": 1.0,
    }
    assert rules.score(line) == pytest.approx(20.0)
