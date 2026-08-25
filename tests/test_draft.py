"""Draft engine tests: survival model, roster need, and the recommender."""
from __future__ import annotations

import pytest

from src.draft.recommender import DraftRecommender, RosterState, build_tier_index
from src.draft.survival import (
    DraftPosition,
    normal_cdf,
    sigma_for,
    survival_probability,
)
from src.vorp import Board, PlayerValue, assign_tiers, compute_replacement_levels, \
    positions_from_slots, split_slots

SLOTS = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 2, "DEF": 1}


def player(name, pos, points, rank=1, adp=None, tier=1, bye=None, vorp=None):
    p = PlayerValue(
        player_key=f"{name}|{pos}", name=name, position=pos, team="XX",
        points=points, adp=adp, bye_week=bye,
    )
    p.position_rank = rank
    p.tier = tier
    p.vorp = points if vorp is None else vorp
    return p


# --- snake draft order -------------------------------------------------------

def test_snake_pick_numbers():
    pos = DraftPosition(num_teams=12, draft_slot=3, rounds=4)
    # R1 pick 3, R2 reverses to slot 10 -> pick 22, R3 pick 27, R4 pick 46
    assert pos.picks() == [3, 22, 27, 46]


def test_first_slot_snake():
    pos = DraftPosition(num_teams=12, draft_slot=1, rounds=3)
    assert pos.picks() == [1, 24, 25]


def test_next_pick_and_distance():
    pos = DraftPosition(num_teams=12, draft_slot=1, rounds=3)
    assert pos.next_pick_after(1) == 24
    assert pos.picks_until_next(1) == 23
    assert pos.next_pick_after(25) is None


def test_current_round():
    pos = DraftPosition(num_teams=12, draft_slot=5, rounds=5)
    assert pos.current_round(1) == 1
    assert pos.current_round(12) == 1
    assert pos.current_round(13) == 2


# --- survival model ----------------------------------------------------------

def test_normal_cdf_is_sane():
    assert normal_cdf(0) == pytest.approx(0.5)
    assert normal_cdf(-3) < 0.01
    assert normal_cdf(3) > 0.99


def test_survival_falls_as_the_pick_gets_later():
    early = survival_probability(adp=20, pick_number=10)
    at_adp = survival_probability(adp=20, pick_number=20)
    late = survival_probability(adp=20, pick_number=40)
    assert early > at_adp > late
    # At his own ADP it is a coin flip by construction.
    assert at_adp == pytest.approx(0.5, abs=0.02)


def test_drafted_player_never_survives():
    assert survival_probability(adp=5, pick_number=100, already_drafted=True) == 0.0


def test_player_without_adp_is_treated_as_undrafted():
    assert survival_probability(adp=None, pick_number=50) > 0.9


def test_sigma_grows_with_adp():
    """Pick 3 is predictable; pick 150 is not."""
    assert sigma_for(3, None) < sigma_for(60, None) < sigma_for(200, None)


def test_reported_stdev_is_blended_not_trusted_outright():
    """A suspiciously tight expert spread should not imply certainty."""
    blended = sigma_for(80, adp_stdev=0.5)
    assert blended > 0.5


# --- tiers -------------------------------------------------------------------

def test_tiers_break_at_large_gaps():
    players = [
        player("A", "RB", 300), player("B", "RB", 295), player("C", "RB", 290),
        player("D", "RB", 200),   # big drop -> new tier
        player("E", "RB", 198),
    ]
    tiers = assign_tiers(players, gap_pct=0.08)
    assert len(tiers) == 2
    assert [p.name for p in tiers[0]] == ["A", "B", "C"]
    assert [p.name for p in tiers[1]] == ["D", "E"]


def test_small_absolute_gaps_do_not_create_noise_tiers():
    """Deep in a position, a 1-point gap is noise, not a tier break."""
    players = [player(f"P{i}", "WR", 20 - i * 0.9) for i in range(8)]
    tiers = assign_tiers(players, gap_pct=0.03, min_gap_points=3.0)
    assert len(tiers) == 1


# --- replacement level -------------------------------------------------------

def test_split_slots_separates_flex():
    dedicated, flex = split_slots(SLOTS)
    assert dedicated == {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "DEF": 1}
    assert flex == {"W/R/T": 2}


def test_positions_from_slots_excludes_missing_kicker():
    """This league has no K slot, so kickers must not appear on the board."""
    assert "K" not in positions_from_slots(SLOTS)
    assert set(positions_from_slots(SLOTS)) == {"QB", "RB", "WR", "TE", "DEF"}


def test_superflex_pulls_quarterbacks_into_the_flex():
    slots = {"QB": 1, "RB": 2, "WR": 2, "Q/W/R/T": 1}
    assert "QB" in positions_from_slots(slots)


def test_flex_share_is_simulated_not_assumed():
    """Whichever position has the better flex-range players claims more spots."""
    rbs = [player(f"RB{i}", "RB", 200 - i * 2, rank=i + 1) for i in range(40)]
    # WRs are deliberately better in the flex range than RBs.
    wrs = [player(f"WR{i}", "WR", 210 - i * 1.2, rank=i + 1) for i in range(40)]
    levels = compute_replacement_levels(
        {"RB": rbs, "WR": wrs}, {"RB": 2, "WR": 2, "W/R/T": 2}, num_teams=12
    )
    assert levels["WR"].flex_share > levels["RB"].flex_share
    # `rank` is the rank of the REPLACEMENT player, so it is one past the last
    # starter: 24 dedicated RB starters plus the flex spots RBs claimed, then
    # the next man. The old arithmetic pointed at the last starter himself and
    # understated every VORP by that position's own starter-to-bench gap.
    assert levels["RB"].rank == 24 + levels["RB"].flex_share + 1
    # Total flex spots handed out equals teams x flex slots.
    assert levels["WR"].flex_share + levels["RB"].flex_share == 24


# --- roster need -------------------------------------------------------------

def test_unfilled_starts_at_full_requirement():
    roster = RosterState(starting_slots=SLOTS)
    unfilled = roster.unfilled()
    assert unfilled["RB"] == pytest.approx(2 + 2 / 3)   # 2 dedicated + share of 2 flex
    assert unfilled["QB"] == 1
    assert unfilled["DEF"] == 1


def test_drafting_reduces_the_need():
    roster = RosterState(starting_slots=SLOTS, players=[player("A", "RB", 200)])
    assert roster.unfilled()["RB"] < 2 + 2 / 3


def test_surplus_players_absorb_the_flex():
    roster = RosterState(
        starting_slots=SLOTS,
        players=[player(f"RB{i}", "RB", 200) for i in range(4)],
    )
    unfilled = roster.unfilled()
    assert unfilled["RB"] == 0
    # Two spare RBs cover both flex spots, so WR no longer carries flex need.
    assert unfilled["WR"] == 2


# --- recommender -------------------------------------------------------------

@pytest.fixture
def board():
    players = []
    for i in range(30):
        players.append(player(f"RB{i}", "RB", 250 - i * 5, rank=i + 1, adp=i * 2 + 1,
                              tier=1 + i // 5))
        players.append(player(f"WR{i}", "WR", 240 - i * 4, rank=i + 1, adp=i * 2 + 2,
                              tier=1 + i // 5))
        players.append(player(f"QB{i}", "QB", 300 - i * 3, rank=i + 1, adp=40 + i * 3,
                              tier=1 + i // 5, vorp=60 - i * 3))
        players.append(player(f"TE{i}", "TE", 200 - i * 6, rank=i + 1, adp=50 + i * 4,
                              tier=1 + i // 5))
        players.append(player(f"DEF{i}", "DEF", 130 - i, rank=i + 1, adp=140 + i,
                              tier=1 + i // 5, vorp=12 - i))
    for i, p in enumerate(sorted(players, key=lambda x: -x.vorp), 1):
        p.overall_rank = i
    return Board(players=players, starting_slots=SLOTS, num_teams=12)


def make_recommender(board, slot=1, rounds=14):
    return DraftRecommender(
        board,
        DraftPosition(num_teams=12, draft_slot=slot, rounds=rounds),
        defer_positions=("DEF",),
        defer_until_round=12,
    )


def test_recommender_returns_ranked_results(board):
    rec = make_recommender(board)
    roster = RosterState(starting_slots=SLOTS)
    results = rec.recommend(set(), roster, current_pick=1, top_n=5)
    assert len(results) == 5
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_drafted_players_are_excluded(board):
    rec = make_recommender(board)
    roster = RosterState(starting_slots=SLOTS)
    top = rec.recommend(set(), roster, 1, top_n=1)[0].player
    again = rec.recommend({top.player_key}, roster, 1, top_n=1)[0].player
    assert again.player_key != top.player_key


def test_defense_is_suppressed_early_and_forced_late(board):
    rec = make_recommender(board)
    roster = RosterState(starting_slots=SLOTS)
    early = rec.need_multiplier("DEF", roster, round_number=2)
    late = rec.need_multiplier("DEF", roster, round_number=13)
    assert early < 0.1
    assert late > early


def test_position_cap_stops_a_second_defense(board):
    """A second DEF cannot start and cannot flex; it must never be recommended."""
    rec = make_recommender(board)
    roster = RosterState(starting_slots=SLOTS, players=[player("DEF0", "DEF", 130)])
    assert rec.position_cap("DEF") == 1
    results = rec.recommend(set(), roster, current_pick=160, top_n=20)
    assert all(r.player.position != "DEF" for r in results)


def test_position_cap_allows_rb_depth(board):
    rec = make_recommender(board)
    assert rec.position_cap("RB") > 5


def test_need_multiplier_favours_the_unfilled_position(board):
    rec = make_recommender(board)
    stacked = RosterState(
        starting_slots=SLOTS, players=[player(f"RB{i}", "RB", 200) for i in range(4)]
    )
    assert rec.need_multiplier("WR", stacked, 5) > rec.need_multiplier("RB", stacked, 5)


def test_bye_stack_warning_fires(board):
    rec = make_recommender(board)
    roster = RosterState(
        starting_slots=SLOTS,
        players=[player("A", "RB", 200, bye=7), player("B", "WR", 190, bye=7)],
    )
    target = next(p for p in board.players if p.position == "TE")
    target.bye_week = 7
    results = rec.recommend(set(), roster, 20, top_n=40)
    match = next((r for r in results if r.player.player_key == target.player_key), None)
    assert match is not None
    assert any("bye stack" in w for w in match.warnings)


def test_value_falling_to_you_is_flagged(board):
    """A player whose ADP is far later than his board rank is a value pick."""
    rec = make_recommender(board)
    roster = RosterState(starting_slots=SLOTS)
    results = rec.recommend(set(), roster, 1, top_n=40)
    flagged = [r for r in results if any("value falling" in x for x in r.reasons)]
    assert flagged, "expected at least one falling-value flag"


def test_tier_index_groups_by_position_and_tier(board):
    index = build_tier_index(board.players)
    assert all(
        p.position == pos and p.tier == tier
        for (pos, tier), players in index.items()
        for p in players
    )
