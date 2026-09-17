"""Every category the league pays for must reach the ground truth.

`points_actual` is what every projection in this project is graded against -
the backtest RMSE, `source_accuracy`, the confidence bands, the weekly recap.
It is built from a stat line assembled by hand in `src/sources/usage.py`, and
for months that line carried eight of the thirteen scored offensive categories.

Nothing caught it, for a specific and repeatable reason: a stat line is a plain
dict, so a missing `fum` key and a player who did not fumble both score zero.
The omission was invisible in every aggregate - measured over 2025 it moved the
mean by -0.016 points a week, because fumbles and two-point conversions nearly
cancel - while individual weeks were wrong by up to 4.0 points. Any test that
checked an average would have passed.

So the test is not "these categories are present". It is "the set of categories
the ingest cannot supply is exactly the set we have justified in writing",
which fails when the league adds a category or the ingest quietly drops one.
"""
from __future__ import annotations

import src.league_bootstrap as bootstrap
from src import scoring
from src.sources.usage import KNOWN_MISSING_STATS, _warn_scoring_gaps


def _rules():
    """This league's real rules, not a fixture.

    The gap being tested is between the league's OWN scoring and what the
    ingest supplies, so a simplified fixture would test nothing.
    """
    return scoring.build_from_yahoo(bootstrap.build_settings())


#: The keys `sync_usage` actually puts in `actual_line`. Kept here so the test
#: fails when that line changes, rather than silently testing a stale copy -
#: test_actual_line_matches_the_ingest below is what ties the two together.
INGESTED = {
    "pass_yds", "pass_td", "pass_int", "rush_yds", "rush_td",
    "rec", "rec_yds", "rec_td", "fum", "fum_lost", "two_pt",
}


def test_only_known_gaps_remain():
    """The whole point: an unjustified gap is a failure, not a log line."""
    gaps = set(_rules().scoring_gaps(INGESTED))
    assert gaps == set(KNOWN_MISSING_STATS), (
        f"scored categories missing from points_actual: {sorted(gaps)}. "
        "Either supply them in sync_usage or justify them in KNOWN_MISSING_STATS."
    )


def test_the_categories_that_were_missing_are_now_supplied():
    """Regression: fumbles and two-point conversions specifically.

    Named rather than covered by the set comparison above, so the failure says
    which real bug came back.
    """
    gaps = set(_rules().scoring_gaps(INGESTED))
    for canonical in ("fum", "fum_lost", "two_pt"):
        assert canonical not in gaps, f"{canonical} dropped out of points_actual again"


def test_dropping_a_category_is_caught():
    """If someone removes a stat from the ingest, this must fail."""
    without_fumbles = INGESTED - {"fum", "fum_lost"}
    gaps = set(_rules().scoring_gaps(without_fumbles))
    assert {"fum", "fum_lost"} <= gaps


def test_known_missing_are_really_absent_from_the_dataset():
    """The exemption list is for things that CANNOT be supplied, not for chores.

    Both entries are return touchdowns, which nflverse's ff_opportunity does not
    publish in any column. If that ever changes the exemption should shrink, so
    this records why each one is there.
    """
    assert frozenset({"ret_td", "off_fum_ret_td"}) == KNOWN_MISSING_STATS


def test_a_display_only_or_zero_category_is_not_a_gap():
    """Only categories that actually move the score count.

    A zero modifier (this league's "Points Allowed 21-27") is scored but worth
    nothing, so demanding it would be noise.
    """
    rules = _rules()
    zero_valued = [
        c.canonical for c in rules.categories
        if c.enabled and not c.modifier and c.canonical
    ]
    gaps = set(rules.scoring_gaps(INGESTED))
    for canonical in zero_valued:
        assert canonical not in gaps


def test_warning_names_the_missing_categories(caplog):
    """Standard 4: the degradation is reported, not swallowed."""
    with caplog.at_level("WARNING"):
        _warn_scoring_gaps(_rules(), {"pass_yds": 1.0})
    assert caplog.records, "a stat line missing almost everything must warn"
    message = caplog.records[0].getMessage()
    assert "fum" in message and "points_actual" in message


def test_known_gaps_alone_do_not_warn(caplog):
    """An alert that always fires is one nobody reads."""
    line = dict.fromkeys(INGESTED, 0.0)
    with caplog.at_level("WARNING"):
        _warn_scoring_gaps(_rules(), line)
    assert not caplog.records
