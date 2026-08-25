"""Things that must never be true, whatever the code does.

This suite exists because 280 tests passed while three real bugs shipped. Those
tests all asked the same kind of question - "does this function do what I think
it does?" - and every one of them was answered honestly by code that was wrong,
because the bugs were not in the units. They were in the OUTPUTS:

  * `faab.replacement_levels()` returned 0.0 for every position for weeks. It
    returned. It had a docstring. It had a caller. No test ever asked whether
    the number it produced could possibly be right.
  * A hosted app silently read an empty local SQLite file. Every unit worked.

So these are not unit tests. Each one takes the real pipeline end to end on a
fixed league and asserts a property that CANNOT hold if the model is broken - a
probability outside [0,1], a replacement level of zero, a bid above budget, a
NaN anywhere. They do not care HOW the answer was computed, which is what makes
them survive refactoring: they are the contract, not the implementation.

The rule for adding one: after fixing any bug, ask "what was true of the output
while it was broken, that should never be true of any output?" That sentence,
turned into an assert, goes here. It is then impossible to reintroduce the bug
in any form, including a form nobody has thought of yet.
"""
from __future__ import annotations

import math
from itertools import pairwise

import pytest

from src import vorp
from src.analytics import faab
from src.draft.recommender import DraftRecommender, RosterState
from src.draft.survival import DraftPosition, survival_probability
from tests.league_fixture import NUM_TEAMS, SEASON, SLOTS, build_league


@pytest.fixture(scope="module")
def league(tmp_path_factory):
    path = tmp_path_factory.mktemp("invariants") / "league.db"
    conn, rules = build_league(path)
    yield conn, rules
    conn.close()


@pytest.fixture(scope="module")
def board(league):
    conn, _ = league
    return vorp.build_board(conn, SEASON, SLOTS, NUM_TEAMS)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _every_player(board) -> list:
    return list(board.players)


# --- the board ---------------------------------------------------------------

def test_every_number_on_the_board_is_finite(board):
    """No NaN, no infinity, anywhere.

    A NaN does not raise. It propagates, compares False against everything, and
    silently sorts to an arbitrary place - so a single NaN projection reorders
    the draft board with no error at all. This is the cheapest possible check
    against the most expensive possible failure on draft night.
    """
    bad = []
    for player in _every_player(board):
        for field in ("points", "vorp"):
            value = getattr(player, field, None)
            if value is not None and not _finite(value):
                bad.append(f"{player.name}.{field}={value}")
    assert not bad, f"non-finite values on the board: {bad[:10]}"


def test_replacement_level_is_never_zero_for_a_started_position(board):
    """The exact bug that shipped: 0.0 for every skill position.

    Zero is not a suspicious-looking number, which is why it survived. But a
    replacement level of zero says "the waiver wire is worth nothing", which
    makes VORP equal raw points and quietly turns the whole board into a
    projection re-sort. Any position this league STARTS has a replacement, and
    that replacement scores real points.
    """
    for position in SLOTS:
        if position in ("W/R/T", "BN", "IR"):
            continue
        level = board.replacement.get(position)
        assert level is not None, f"no replacement level computed for {position}"
        assert level.points > 0, (
            f"{position} replacement level is {level.points}. A started position "
            "always has a replaceable player, and he scores more than nothing."
        )


def test_vorp_is_points_minus_replacement(board):
    """VORP must be derivable from its own inputs.

    This catches the case where VORP is computed from one replacement level and
    reported next to a different one - the numbers each look fine alone and
    disagree only when you subtract them.
    """
    for player in _every_player(board):
        level = board.replacement.get(player.position)
        if level is None:
            continue
        assert player.vorp == pytest.approx(player.points - level.points, abs=0.005), (
            f"{player.name}: vorp={player.vorp} but points={player.points} "
            f"and replacement={level.points}"
        )


def test_a_better_projection_never_gets_a_worse_value(board):
    """Within a position, value must be monotonic in points.

    Every ranking bug in this repository showed up here first: an off-by-one in
    the replacement index, a sort applied before a mutation instead of after, a
    tier boundary computed on a stale pool.
    """
    for position in SLOTS:
        players = board.by_position(position)
        for better, worse in pairwise(players):
            assert better.points >= worse.points, (
                f"{position} board is not sorted: {better.name} "
                f"({better.points}) above {worse.name} ({worse.points})"
            )
            assert better.vorp >= worse.vorp, (
                f"{better.name} outprojects {worse.name} but is worth less"
            )


def test_quarterbacks_outscore_running_backs_and_are_still_worth_less(board):
    """The single sentence that justifies this whole application existing.

    The fixture gives quarterbacks the highest raw totals in the league. If the
    board ranks them first anyway, positional value is not being applied and
    every recommendation downstream is wrong - which is exactly what a naive
    points-sorted board does.
    """
    best_qb = board.by_position("QB")[0]
    best_rb = board.by_position("RB")[0]
    assert best_qb.points > best_rb.points, "fixture no longer poses the question"
    assert best_qb.vorp < best_rb.vorp, (
        f"the board ranks the top QB ({best_qb.vorp:.1f} VORP) above the top RB "
        f"({best_rb.vorp:.1f}) despite replacement-level QBs being abundant. "
        "Positional value is not being applied."
    )


# --- probabilities -----------------------------------------------------------

@pytest.mark.parametrize("adp", [1.0, 12.0, 45.0, 120.0, 300.0])
@pytest.mark.parametrize("pick", [1, 5, 22, 90, 200])
def test_every_survival_probability_is_a_probability(adp, pick):
    """[0, 1]. Always. Including the conditional form.

    Conditioning is a division, and a division is where probabilities escape
    their range. S(next)/S(current) exceeds 1 the moment the denominator is
    computed from a different distribution than the numerator.
    """
    for current in (None, 1, pick, pick + 30):
        p = survival_probability(adp, pick, adp_stdev=8.0, current_pick=current)
        assert _finite(p), f"survival is {p} for adp={adp} pick={pick}"
        assert 0.0 <= p <= 1.0, (
            f"survival probability {p} for adp={adp}, pick={pick}, "
            f"current={current} is not a probability"
        )


def test_survival_never_increases_with_a_later_pick():
    """Waiting cannot make a player MORE likely to be there."""
    previous = 1.0
    for pick in range(5, 200, 5):
        p = survival_probability(30.0, pick, adp_stdev=8.0, current_pick=1)
        assert p <= previous + 1e-9, (
            f"survival rose from {previous} to {p} at pick {pick}"
        )
        previous = p


def test_a_drafted_player_never_survives():
    assert survival_probability(10.0, 50, already_drafted=True) == 0.0


# --- the recommender ---------------------------------------------------------

@pytest.fixture(scope="module")
def recommender(board):
    return DraftRecommender(board, DraftPosition(num_teams=NUM_TEAMS, draft_slot=3, rounds=15))


def test_it_never_recommends_a_player_who_is_gone(recommender, board):
    """The most embarrassing possible failure, and a one-line assert.

    `drafted` is a set of keys; a recommender that compares against names, or
    against a stale copy, hands you a player another team already owns.
    """
    everyone = _every_player(board)
    gone = {p.player_key for p in everyone[:40]}
    picks = recommender.recommend(gone, RosterState(SLOTS), current_pick=41, top_n=10)
    assert picks, "no recommendations at all with 40 players off the board"
    for pick in picks:
        assert pick.player.player_key not in gone, (
            f"recommended {pick.player.name}, who is already drafted"
        )


def test_it_always_returns_something_until_the_board_is_empty(recommender, board):
    """Silence is the failure mode that looks like a working app.

    An empty recommendation list renders as an empty panel, which reads as "no
    good options" rather than "this is broken" - and on a 90-second clock nobody
    investigates. Every pick in a 15-round draft must produce a suggestion.
    """
    ordered = sorted(_every_player(board), key=lambda p: p.vorp, reverse=True)
    gone: set[str] = set()
    for pick_number in range(1, 12 * 15, 7):
        picks = recommender.recommend(gone, RosterState(SLOTS), pick_number, top_n=5)
        assert picks, f"no recommendation at pick {pick_number} with {len(gone)} gone"
        gone.update(p.player_key for p in ordered[:pick_number])


def test_recommendation_scores_are_finite_and_ordered(recommender):
    picks = recommender.recommend(set(), RosterState(SLOTS), current_pick=3, top_n=20)
    scores = [p.score for p in picks]
    assert all(_finite(s) for s in scores), f"non-finite score in {scores}"
    assert scores == sorted(scores, reverse=True), (
        "recommendations are not returned best-first"
    )


def test_an_empty_roster_needs_every_starting_slot():
    """Need must be read off the slots, not hardcoded.

    A league with no kicker slot must never be told it needs a kicker - which is
    this league, and which is a defect that only shows in a league like it.
    """
    unfilled = RosterState(SLOTS).unfilled()
    assert "K" not in unfilled, "asked for a kicker in a league with no kicker slot"
    for position in SLOTS:
        if position == "W/R/T":
            continue
        assert unfilled.get(position, 0) > 0, (
            f"an empty roster does not need a {position}"
        )


# --- FAAB --------------------------------------------------------------------

def test_the_waiver_replacement_levels_are_never_all_zero(league):
    """The original bug, guarded at its own front door.

    `faab.replacement_levels()` is a SECOND replacement-level implementation,
    separate from the board's, because in season the replacement is whoever is
    actually on the wire rather than a theoretical rank. It returned 0.0 for
    every offensive position for weeks - the query had no join to `rosters`, so
    it took the median over every player with a stored line, most of whom
    project nothing.

    The first version of this suite tested the board's replacement levels and
    would have missed a reintroduction of exactly this bug, because the two
    implementations share no code. Both front doors now have a guard.
    """
    conn, _ = league
    levels = faab.replacement_levels(
        conn, SEASON, starting_slots=SLOTS, num_teams=NUM_TEAMS
    )
    assert levels, "no replacement levels returned at all"

    started = [pos for pos in SLOTS if pos not in ("W/R/T", "BN", "IR")]
    zeroed = [pos for pos in started if not levels.get(pos)]
    assert not zeroed, (
        f"waiver replacement level is zero for {zeroed}. A zero says the wire "
        "is worth nothing, which makes every waiver value equal a gross "
        "projection and a backup out-value a starter."
    )
    for position, points in levels.items():
        assert _finite(points), f"{position} replacement level is {points}"
        assert points >= 0, f"{position} replacement level is negative: {points}"


def test_the_two_replacement_models_broadly_agree(league, board):
    """They are computed differently and must still land in the same world.

    The board ranks by projected season points; the waiver model reads the
    actual wire. They will not match, and should not - but an order-of-magnitude
    gap means one of them is measuring something other than what it claims.
    """
    conn, _ = league
    waiver = faab.replacement_levels(
        conn, SEASON, starting_slots=SLOTS, num_teams=NUM_TEAMS
    )
    for position in ("QB", "RB", "WR", "TE"):
        level = board.replacement.get(position)
        other = waiver.get(position)
        if not level or not other:
            continue
        ratio = other / level.points
        assert 0.2 <= ratio <= 5.0, (
            f"{position}: the board says replacement is {level.points:.1f} and "
            f"the waiver model says {other:.1f} - a {ratio:.1f}x gap. One of "
            "them is not measuring replacement level."
        )



def _rivals(n: int = 11) -> list:
    return [
        faab.ManagerProfile(
            team_key=f"t{i}", name=f"T{i}", observations=6,
            beta=0.55, raw_beta=0.55, mean_bid=14.0, max_bid=42,
            budget_left=60, auctions_seen=20,
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("budget", [1, 7, 45, 100])
@pytest.mark.parametrize("value", [0.0, 3.0, 25.0, 90.0])
def test_a_bid_never_exceeds_the_budget(budget, value):
    """You cannot spend money you do not have.

    Yahoo rejects an over-budget claim outright, so this is not a rounding
    nicety - it is the difference between winning a player and losing the claim
    entirely while believing you bid.
    """
    advice = faab.recommend(value=value, my_budget=budget, rivals=_rivals(), weeks_left=8)
    assert 0 <= advice.recommended <= budget, f"bid {advice.recommended} against a budget of {budget}"
    assert isinstance(advice.recommended, int), "Yahoo takes whole dollars"


def test_more_valuable_players_are_bid_more_on():
    """Bids must discriminate.

    Before this was fixed the model returned the same number for everyone, which
    is not a bidding strategy - it is a constant with a docstring.
    """
    bids = [
        faab.recommend(value=v, my_budget=100, rivals=_rivals(), weeks_left=8).recommended
        for v in (2.0, 12.0, 40.0, 85.0)
    ]
    assert bids == sorted(bids), f"bids do not rise with value: {bids}"
    assert len(set(bids)) > 2, f"bids barely discriminate: {bids}"


def test_win_probability_is_a_probability():
    for bid in range(0, 101, 5):
        p = faab.win_probability(bid, rivals=_rivals(), value=40.0)
        assert 0.0 <= p <= 1.0, f"win probability {p} for a ${bid} bid"


def test_bidding_more_never_lowers_your_chance_of_winning():
    previous = -1.0
    for bid in range(0, 101, 5):
        p = faab.win_probability(bid, rivals=_rivals(), value=40.0)
        assert p >= previous - 1e-9, f"raising the bid to ${bid} lowered win probability"
        previous = p
