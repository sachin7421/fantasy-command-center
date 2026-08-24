"""Scoring engine tests against hand-computed point totals (spec 9, 10.1).

Each expected value below is worked out by hand in the comment above it, so a
regression shows up as a disagreement with arithmetic rather than with a
previously-recorded number.
"""
from __future__ import annotations

import pytest

from src import scoring


@pytest.fixture
def rules(yahoo_settings):
    return scoring.build_from_yahoo(yahoo_settings)


# --- category parsing --------------------------------------------------------

def test_categories_resolve_to_canonical_keys(rules):
    by_id = {c.stat_id: c for c in rules.categories}
    assert by_id[4].canonical == "pass_yds"
    assert by_id[5].canonical == "pass_td"
    assert by_id[11].canonical == "rec"
    assert by_id[19].canonical == "fg_0_19"
    assert by_id[23].canonical == "fg_50p"


def test_offense_and_defense_share_names_but_not_meaning(rules):
    """Yahoo calls both stat 6 and stat 33 "Int"; they must not collide."""
    by_id = {c.stat_id: c for c in rules.categories}
    assert by_id[6].canonical == "pass_int"      # offense: thrown
    assert by_id[33].canonical == "def_int"      # defense: caught
    assert by_id[6].modifier == -2
    assert by_id[33].modifier == 2


def test_points_allowed_buckets_parse_ranges(rules):
    by_id = {c.stat_id: c for c in rules.categories}
    shutout, mid, blowout = by_id[50], by_id[52], by_id[56]
    assert (shutout.bucket_low, shutout.bucket_high) == (0.0, 0.0)
    assert (mid.bucket_low, mid.bucket_high) == (7.0, 13.0)
    assert (blowout.bucket_low, blowout.bucket_high) == (35.0, None)
    assert blowout.matches_bucket(41) and not blowout.matches_bucket(34)


def test_display_only_category_is_not_scored(rules):
    by_id = {c.stat_id: c for c in rules.categories}
    assert by_id[31].display_only is True


# --- scoring: offense --------------------------------------------------------

def test_quarterback_line(rules):
    # 312 pass yds * 0.04 = 12.48
    # 2 pass TD    * 4    =  8.00
    # 1 INT        * -2   = -2.00
    # 24 rush yds  * 0.1  =  2.40
    # 1 rush TD    * 6    =  6.00
    #                       ------
    #                        26.88
    line = {
        "pass_yds": 312, "pass_td": 2, "pass_int": 1,
        "rush_att": 5, "rush_yds": 24, "rush_td": 1,
    }
    assert rules.score(line) == pytest.approx(26.88)


def test_running_back_half_ppr(rules):
    # 118 rush yds * 0.1  = 11.80
    # 1 rush TD    * 6    =  6.00
    # 4 rec        * 0.5  =  2.00
    # 31 rec yds   * 0.1  =  3.10
    # 1 fum lost   * -2   = -2.00
    #                       ------
    #                        20.90
    line = {
        "rush_att": 22, "rush_yds": 118, "rush_td": 1,
        "rec": 4, "rec_yds": 31, "fum_lost": 1,
    }
    assert rules.score(line) == pytest.approx(20.90)


def test_receptions_come_from_league_settings_not_a_constant(yahoo_settings, ppr_settings):
    """Same stat line, two leagues: the 6 receptions are worth 3.0 vs 6.0."""
    line = {"rec": 6, "rec_yds": 84, "rec_td": 1}
    half = scoring.build_from_yahoo(yahoo_settings)
    full = scoring.build_from_yahoo(ppr_settings)

    # half PPR: 6*0.5 + 84*0.1 + 6 = 3.0 + 8.4 + 6 = 17.4
    assert half.score(line) == pytest.approx(17.4)
    # full PPR: 6*1.0 + 8.4 + 6 = 20.4
    assert full.score(line) == pytest.approx(20.4)
    assert half.ppr_value == 0.5 and full.ppr_value == 1.0
    assert half.is_ppr and full.is_ppr


def test_two_point_conversion_and_return_td(rules):
    # 1 ret TD * 6 = 6.00 ; 1 2-PT * 2 = 2.00 ; 45 rec yds * 0.1 = 4.50
    # 2 rec * 0.5 = 1.00  => 13.50
    line = {"ret_td": 1, "two_pt": 1, "rec": 2, "rec_yds": 45}
    assert rules.score(line) == pytest.approx(13.50)


# --- scoring: kicker ---------------------------------------------------------

def test_kicker_scores_by_distance(rules):
    # FG 20-29 x1 *3 = 3 ; FG 40-49 x2 *4 = 8 ; FG 50+ x1 *5 = 5
    # PAT made x3 *1 = 3 ; PAT miss x1 *-1 = -1  => 18
    line = {
        "fg_20_29": 1, "fg_40_49": 2, "fg_50p": 1,
        "pat_made": 3, "pat_miss": 1,
    }
    assert rules.score(line) == pytest.approx(18.0)


def test_bare_fg_total_is_expanded_across_distances(rules):
    """A source giving only `fg_made` still scores, via the distance prior."""
    expanded = scoring.expand_fg_distances({"fg_made": 3, "pat_made": 2})
    assert expanded["_fg_distance_estimated"] == 1
    assert sum(expanded[k] for k in scoring.FG_DISTANCE_KEYS) == pytest.approx(3.0)
    # 3 FGs blended across the prior lands between the cheapest and dearest bucket.
    points = rules.score(expanded)
    assert 2 + 3 * 3 <= points <= 2 + 3 * 5


# --- scoring: defense / special teams ---------------------------------------

def test_defense_with_points_allowed_bucket(rules):
    # 3 sack *1 = 3 ; 2 int *2 = 4 ; 1 fum rec *2 = 2 ; 1 def TD *6 = 6
    # 10 points allowed lands in the 7-13 bucket = 4
    #                                              -> 19
    line = {
        "def_sack": 3, "def_int": 2, "def_fum_rec": 1,
        "def_td": 1, "def_pts_allowed": 10,
    }
    assert rules.score(line) == pytest.approx(19.0)


def test_shutout_and_blowout_buckets_are_exclusive(rules):
    base = {"def_sack": 2}
    shutout = rules.score({**base, "def_pts_allowed": 0})
    blowout = rules.score({**base, "def_pts_allowed": 38})
    # 2 sacks = 2 ; shutout bucket +10 ; 35+ bucket -4
    assert shutout == pytest.approx(12.0)
    assert blowout == pytest.approx(-2.0)


@pytest.mark.parametrize("allowed", [0, 3, 6, 7, 13, 14, 20, 21, 27, 28, 34, 35, 52])
def test_exactly_one_points_allowed_bucket_matches(rules, allowed):
    """The buckets must tile the whole range with no gaps and no overlaps."""
    matching = [
        c for c in rules.categories
        if c.bucket_base == "def_pts_allowed" and c.matches_bucket(allowed)
    ]
    assert len(matching) == 1, f"{allowed} matched {[c.display_name for c in matching]}"


def test_bucket_appears_once_in_breakdown(rules):
    detail = rules.breakdown({"def_pts_allowed": 17})
    assert [k for k in detail if k.startswith("Pts Allow")] == ["Pts Allow 14-20"]
    # The 21-27 bucket is worth 0 here, so it drops out as a no-op contribution.
    assert not [k for k in rules.breakdown({"def_pts_allowed": 21}) if k.startswith("Pts")]


# --- breakdown / explainability ---------------------------------------------

def test_breakdown_sums_to_score(rules):
    line = {"pass_yds": 287, "pass_td": 3, "pass_int": 2, "rush_yds": 11}
    detail = rules.breakdown(line)
    assert sum(detail.values()) == pytest.approx(rules.score(line))
    assert detail["Pass TD"] == pytest.approx(12.0)
    assert detail["Int"] == pytest.approx(-4.0)


def test_unscored_stats_are_ignored(rules):
    """Targets are not a scoring category here; they must contribute nothing."""
    with_targets = rules.score({"rec": 5, "rec_yds": 60, "rec_tgt": 11})
    without = rules.score({"rec": 5, "rec_yds": 60})
    assert with_targets == without


def test_position_type_filter_scopes_categories(rules):
    line = {"def_sack": 3, "def_pts_allowed": 10}
    assert rules.score(line, position_type="DT") == pytest.approx(rules.score(line))
    # Scored as an offensive player, the defensive categories do not apply.
    assert rules.score(line, position_type="O") == pytest.approx(0.0)


# --- bonuses -----------------------------------------------------------------

def test_threshold_bonus_applies_at_target(yahoo_settings):
    settings = dict(yahoo_settings)
    settings["stat_modifiers"] = dict(settings["stat_modifiers"])
    settings["stat_modifiers"]["bonuses"] = [
        {"bonus": {"stat_id": 9, "target": 100, "points": 3}}
    ]
    rules = scoring.build_from_yahoo(settings)
    assert rules.bonuses and rules.bonuses[0].canonical == "rush_yds"
    # 99 yds -> 9.9, no bonus. 100 yds -> 10.0 + 3 = 13.0
    assert rules.score({"rush_yds": 99}) == pytest.approx(9.9)
    assert rules.score({"rush_yds": 100}) == pytest.approx(13.0)


# --- round tripping ----------------------------------------------------------

def test_scoring_survives_json_round_trip(rules):
    restored = scoring.LeagueScoring.from_json(rules.to_json())
    line = {"pass_yds": 250, "pass_td": 2, "rec": 3}
    assert restored.score(line) == pytest.approx(rules.score(line))
    assert restored.active_canonicals() == rules.active_canonicals()


def test_league_with_no_modifiers_scores_zero():
    rules = scoring.build_from_yahoo({"stat_categories": {"stats": []}})
    assert rules.score({"pass_yds": 400, "pass_td": 5}) == 0.0
