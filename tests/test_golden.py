"""The numbers this model produced yesterday, frozen.

The invariant suite next door proves an output is not ABSURD. This one proves
it has not CHANGED. They catch opposite halves of the same problem: a bid of
$47 turning into $46 breaks no invariant at all, and is exactly what a silently
mis-wired refactor looks like.

How it works: the whole pipeline runs on the fixed league in `league_fixture`,
and every number it produces is written to `tests/golden/*.json`. On every later
run the numbers are recomputed and compared. A diff means the model's behaviour
moved. That is not automatically bad - most of the time it is the intended
result of the change being made - but it must never happen SILENTLY.

    When a diff is intended, bless it deliberately:

        python -m pytest tests/test_golden.py --update-golden
        git diff tests/golden/          # <- read this before committing

The second line is the entire point. `git diff` on a golden file is a plain-text
statement of what a change did to every recommendation, tier, bid and
probability in the application - including the parts of it the author was not
thinking about. A change to the shrinkage constant that was only meant to affect
waivers, and turns out to reorder round one, shows up there and nowhere else.

Rules for this file:

  * Never `--update-golden` to make a red build green. Read the diff first. If
    it cannot be explained in a sentence, the change is wrong, not the golden.
  * Round hard (2 dp). Floating point differs in the last bits across platforms
    and a gate that fails on CI but not locally gets switched off within a week.
  * Nothing here may read a clock, a network, or a random seed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import vorp
from src.analytics import faab
from src.draft.recommender import DraftRecommender, RosterState
from src.draft.survival import DraftPosition, survival_probability, wait_cost
from tests.league_fixture import NUM_TEAMS, SEASON, SLOTS, build_league

GOLDEN = Path(__file__).parent / "golden"


def _round(value):
    """Round every float in a nested structure to 2 dp.

    Platform float noise in the 15th digit is not a behaviour change, and a
    gate that cries wolf about it is a gate somebody turns off.
    """
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v) for v in value]
    return value


def _check(name: str, produced, request) -> None:
    """Compare against the stored snapshot, or write it when blessed."""
    path = GOLDEN / f"{name}.json"
    produced = _round(produced)

    if request.config.getoption("--update-golden"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        pytest.skip(f"golden/{name}.json rewritten - read `git diff` before committing")

    if not path.exists():
        pytest.fail(
            f"tests/golden/{name}.json does not exist. Generate it with\n"
            f"    python -m pytest tests/test_golden.py --update-golden\n"
            "and commit it after reading the diff."
        )

    expected = json.loads(path.read_text(encoding="utf-8"))
    assert produced == expected, (
        f"{name} no longer matches its golden snapshot.\n"
        "The model's behaviour changed. If that was intended, re-bless it with\n"
        "    python -m pytest tests/test_golden.py --update-golden\n"
        "and READ `git diff tests/golden/` before you commit it."
    )


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    conn, _ = build_league(tmp_path_factory.mktemp("golden") / "league.db")
    try:
        yield vorp.build_board(conn, SEASON, SLOTS, NUM_TEAMS)
    finally:
        conn.close()


# --- the valued board --------------------------------------------------------

def test_the_top_of_the_board_is_unchanged(board, request):
    """The first 40 names, in order, with their values.

    This is the single most consequential list the application produces: it is
    what is on screen when the draft clock is running.
    """
    top = sorted(board.players, key=lambda p: p.vorp, reverse=True)[:40]
    _check("board_top_40", [
        {"name": p.name, "pos": p.position, "points": p.points,
         "vorp": p.vorp, "tier": p.tier, "rank": p.overall_rank}
        for p in top
    ], request)


def test_replacement_levels_are_unchanged(board, request):
    _check("replacement_levels", {
        pos: {"points": level.points, "rank": level.rank,
              "scarce": getattr(level, "scarce", None)}
        for pos, level in sorted(board.replacement.items())
    }, request)


def test_positional_scarcity_is_unchanged(board, request):
    _check("scarcity", dict(sorted(board.scarcity.items())), request)


def test_tier_boundaries_are_unchanged(board, request):
    """Where each tier breaks, by name.

    Tiers drive the "take him now or he is gone" call. A change here that is
    not intended is a change to when the app tells you to panic.
    """
    _check("tiers", {
        pos: [[p.name for p in tier] for tier in tiers]
        for pos, tiers in sorted(board.tiers.items())
    }, request)


# --- the recommender ---------------------------------------------------------

def test_the_recommendations_at_every_one_of_my_picks_are_unchanged(board, request):
    """A full simulated draft from the 3 slot, recorded pick by pick.

    Players come off the board in ADP order between my picks, so this exercises
    need, tier urgency and survival together at every round - the interaction
    no unit test covers, and where the numbers actually come from.
    """
    position = DraftPosition(num_teams=NUM_TEAMS, draft_slot=3, rounds=15)
    recommender = DraftRecommender(board, position)

    by_adp = sorted(
        board.players,
        key=lambda p: (p.adp if p.adp is not None else 9999.0, p.name),
    )
    roster = RosterState(SLOTS)
    gone: set[str] = set()
    snapshot = []

    for overall in position.picks():
        # everyone taken before this pick goes, in market order
        for player in by_adp:
            if len(gone) >= overall - 1:
                break
            if player.player_key not in gone:
                gone.add(player.player_key)

        picks = recommender.recommend(gone, roster, overall, top_n=3)
        snapshot.append({
            "pick": overall,
            "round": position.current_round(overall),
            "choices": [
                {"name": r.player.name, "pos": r.player.position,
                 "score": r.score, "vorp": r.vorp,
                 "need": r.need_multiplier, "urgency": r.tier_urgency,
                 "survival": r.survival, "reasons": r.reasons,
                 "warnings": r.warnings}
                for r in picks
            ],
        })

        # take the top recommendation and carry on
        taken = picks[0].player
        gone.add(taken.player_key)
        roster.players.append(taken)

    _check("draft_from_slot_3", snapshot, request)


# --- probabilities and money -------------------------------------------------

def test_survival_probabilities_are_unchanged(request):
    _check("survival", [
        {"adp": adp, "at": pick, "from": current,
         "p": survival_probability(adp, pick, adp_stdev=sd, current_pick=current)}
        for adp in (5.0, 20.0, 45.0, 90.0)
        for sd in (4.0, 12.0)
        for current, pick in ((1, 22), (3, 22), (22, 27), (27, 46))
    ], request)


def test_wait_cost_is_unchanged(board, request):
    """What each position costs you to pass on, at each of my picks.

    This is the number behind "take the running back now" - the expected value
    lost by waiting a full turn of the snake.
    """
    position = DraftPosition(num_teams=NUM_TEAMS, draft_slot=3, rounds=15)
    out = []
    for pos in ("RB", "WR", "TE", "QB"):
        candidates = [
            (p.name, p.vorp, p.adp, p.adp_stdev)
            for p in board.by_position(pos)[:12]
        ]
        for pick in (3, 22, 27, 46):
            nxt = position.next_pick_after(pick) or pick + 20
            costs = wait_cost(candidates, nxt)
            out.append({
                "pos": pos, "at": pick, "next": nxt,
                "cost": dict(sorted(costs.items())),
            })
    _check("wait_cost", out, request)


def test_faab_advice_is_unchanged(request):
    """The bid table, across value and budget.

    This is the model that returned $60 for every player until it was fixed.
    Freezing the whole surface makes that class of collapse impossible to
    reintroduce quietly - a flat column here is visible at a glance.
    """
    rivals = [
        faab.ManagerProfile(
            team_key=f"t{i}", name=f"T{i}", observations=4 + i,
            beta=0.4 + 0.05 * i, raw_beta=0.4 + 0.05 * i,
            mean_bid=8.0 + 2.0 * i, max_bid=20 + 3 * i,
            budget_left=100 - 7 * i, auctions_seen=18 + i,
        )
        for i in range(11)
    ]
    _check("faab_advice", [
        {
            "value": value, "budget": budget,
            **{
                k: getattr(faab.recommend(value=value, my_budget=budget,
                                          rivals=rivals, weeks_left=weeks), k)
                for k in ("recommended", "worth_to_you", "price_to_win",
                          "win_probability", "min_competitive", "walk_away")
            },
            "weeks_left": weeks,
        }
        for value in (2.0, 8.0, 20.0, 45.0, 80.0)
        for budget in (10, 50, 100)
        for weeks in (3, 10)
    ], request)
