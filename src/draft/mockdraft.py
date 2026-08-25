"""Mock draft simulation (spec 9 phase 3, acceptance test 10.3).

Two jobs:
  1. Let me rehearse a draft from my real slot against ADP-following opponents.
  2. Prove the recommender actually beats naive ADP drafting, measured in
     projected starting-lineup points across many simulated drafts.

Opponents follow ADP with noise, which is a fair model of a casual Yahoo league
and a deliberately strong baseline: ADP encodes the entire market's opinion.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

from src.draft.recommender import DraftRecommender, RosterState
from src.draft.survival import DraftPosition
from src.lineup_solver import best_lineup
from src.vorp import Board, PlayerValue, split_slots

#: Players with no ADP are effectively undrafted; park them past the last pick.
NO_ADP = 400.0


@dataclass
class SimTeam:
    team_id: int
    is_me: bool
    players: list[PlayerValue] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.players:
            out[p.position] = out.get(p.position, 0) + 1
        return out


@dataclass
class SimResult:
    my_points: float
    opponent_points: list[float]
    my_roster: list[PlayerValue]
    my_rank: int

    @property
    def opponent_mean(self) -> float:
        return statistics.mean(self.opponent_points) if self.opponent_points else 0.0

    @property
    def edge(self) -> float:
        return self.my_points - self.opponent_mean


def _required_remaining(team: SimTeam, starting_slots: dict[str, int]) -> dict[str, int]:
    """Dedicated starting slots this team still has to fill."""
    dedicated, _ = split_slots(starting_slots)
    counts = team.counts()
    return {
        pos: max(0, need - counts.get(pos, 0))
        for pos, need in dedicated.items()
        if max(0, need - counts.get(pos, 0)) > 0
    }


def adp_pick(
    available: list[PlayerValue],
    team: SimTeam,
    starting_slots: dict[str, int],
    rounds_left: int,
    rng: random.Random,
    noise: float = 6.0,
) -> PlayerValue:
    """One naive-ADP opponent pick, with noise and late-round need filling."""
    needs = _required_remaining(team, starting_slots)

    # If there are exactly as many picks left as unfilled starters, stop being
    # cute and fill them - this is what real managers do.
    if needs and sum(needs.values()) >= rounds_left:
        pool = [p for p in available if p.position in needs] or available
    else:
        # Do not stockpile a position far beyond what is startable. Real managers
        # do not draft four defenses, and letting the baseline do so would make
        # it a strawman rather than a fair comparison.
        counts = team.counts()
        dedicated, _ = split_slots(starting_slots)
        caps = {
            "DEF": max(1, dedicated.get("DEF", 1)),
            "K": max(1, dedicated.get("K", 1)),
            "QB": max(2, dedicated.get("QB", 1) + 1),
            "TE": max(2, dedicated.get("TE", 1) + 1),
        }
        pool = [
            p for p in available if counts.get(p.position, 0) < caps.get(p.position, 8)
        ] or available

    scored = [
        ((p.adp if p.adp is not None else NO_ADP) + rng.gauss(0, noise), p)
        for p in pool
    ]
    scored.sort(key=lambda kv: kv[0])
    return scored[0][1]


def simulate_draft(
    board: Board,
    starting_slots: dict[str, int],
    num_teams: int = 12,
    rounds: int = 15,
    my_slot: int = 1,
    seed: int | None = None,
    use_recommender: bool = True,
    need_weight: float = 0.35,
    defer_positions: tuple[str, ...] = ("K", "DEF"),
    defer_until_round: int = 12,
    opponent_noise: float = 6.0,
    bye_stack_threshold: int = 3,
    te_flex_credit: int = 0,
) -> SimResult:
    """Run one full snake draft and score every resulting starting lineup."""
    rng = random.Random(seed)
    position = DraftPosition(num_teams=num_teams, draft_slot=my_slot, rounds=rounds)
    teams = {i: SimTeam(team_id=i, is_me=(i == my_slot)) for i in range(1, num_teams + 1)}

    # Every knob the live draft reads, so a rehearsal is a rehearsal of the
    # thing that will actually run. Two of these were missing, which meant
    # changing them in config.yaml changed the draft but not the mock of it.
    recommender = DraftRecommender(
        board,
        position,
        need_weight=need_weight,
        defer_positions=defer_positions,
        defer_until_round=defer_until_round,
        bye_stack_threshold=bye_stack_threshold,
        te_flex_credit=te_flex_credit,
    )

    drafted: set[str] = set()
    # A pool trimmed to what could plausibly go; keeps each sim fast.
    pool = sorted(board.players, key=lambda p: p.vorp, reverse=True)[
        : max(num_teams * rounds * 3, 300)
    ]

    for overall in range(1, num_teams * rounds + 1):
        rnd = (overall - 1) // num_teams + 1
        slot_in_round = (overall - 1) % num_teams + 1
        team_id = slot_in_round if rnd % 2 == 1 else num_teams - slot_in_round + 1
        team = teams[team_id]
        available = [p for p in pool if p.player_key not in drafted]
        if not available:
            break
        rounds_left = rounds - rnd + 1

        if team.is_me and use_recommender:
            roster = RosterState(starting_slots=starting_slots, players=team.players)
            recs = recommender.recommend(drafted, roster, overall, top_n=1)
            choice = recs[0].player if recs else adp_pick(
                available, team, starting_slots, rounds_left, rng, opponent_noise
            )
        else:
            choice = adp_pick(
                available, team, starting_slots, rounds_left, rng, opponent_noise
            )

        drafted.add(choice.player_key)
        team.players.append(choice)

    totals = {
        tid: best_lineup(t.players, starting_slots).total for tid, t in teams.items()
    }
    my_points = totals[my_slot]
    opponents = [v for k, v in totals.items() if k != my_slot]
    rank = 1 + sum(1 for v in opponents if v > my_points)

    return SimResult(
        my_points=my_points,
        opponent_points=opponents,
        my_roster=teams[my_slot].players,
        my_rank=rank,
    )


@dataclass
class Comparison:
    n: int
    recommender_mean: float
    baseline_mean: float
    win_rate: float
    mean_edge: float
    recommender_rank: float
    baseline_rank: float

    @property
    def beats_baseline(self) -> bool:
        return self.recommender_mean > self.baseline_mean


def compare_strategies(
    board: Board,
    starting_slots: dict[str, int],
    n: int = 100,
    num_teams: int = 12,
    rounds: int = 15,
    my_slot: int | None = None,
    seed: int = 1234,
    **kwargs,
) -> Comparison:
    """Acceptance test 10.3: does the assistant beat naive ADP drafting?

    Each trial runs the same draft seed twice - once with the recommender in my
    seat, once with a naive ADP drafter - so the two strategies face identical
    opponent behaviour and the difference is attributable to the strategy.
    """
    rec_scores: list[float] = []
    base_scores: list[float] = []
    rec_ranks: list[int] = []
    base_ranks: list[int] = []
    wins = 0

    for i in range(n):
        trial_seed = seed + i
        slot = my_slot if my_slot else (i % num_teams) + 1

        with_rec = simulate_draft(
            board, starting_slots, num_teams, rounds, slot,
            seed=trial_seed, use_recommender=True, **kwargs,
        )
        without = simulate_draft(
            board, starting_slots, num_teams, rounds, slot,
            seed=trial_seed, use_recommender=False, **kwargs,
        )

        rec_scores.append(with_rec.my_points)
        base_scores.append(without.my_points)
        rec_ranks.append(with_rec.my_rank)
        base_ranks.append(without.my_rank)
        if with_rec.my_points > without.my_points:
            wins += 1

    return Comparison(
        n=n,
        recommender_mean=round(statistics.mean(rec_scores), 2),
        baseline_mean=round(statistics.mean(base_scores), 2),
        win_rate=round(wins / n, 3),
        mean_edge=round(statistics.mean(r - b for r, b in zip(rec_scores, base_scores, strict=True)), 2),
        recommender_rank=round(statistics.mean(rec_ranks), 2),
        baseline_rank=round(statistics.mean(base_ranks), 2),
    )
