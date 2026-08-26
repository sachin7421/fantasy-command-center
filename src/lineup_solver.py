"""Optimal starting-lineup assignment.

Used by the season lineup optimizer (spec 6.3) and to evaluate drafted teams
(spec 10.3). Rosters are tiny, so we solve this exactly rather than greedily.

Greedy slot-filling is genuinely wrong here: filling FLEX with the best
available player can strand a WR-only player with no WR slot left. This is a
maximum-weight bipartite assignment, solved with a DP over the bitmask of filled
slots - exact, and instant at roster scale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar
from collections.abc import Callable, Iterable, Sequence

from src.vorp import BENCH_SLOTS, DEFENSIVE_SLOTS, FLEX_ELIGIBILITY


def slot_accepts(slot: str, position: str) -> bool:
    """Can `position` legally start in `slot`?"""
    s = slot.upper()
    if s in BENCH_SLOTS:
        return False
    if s in FLEX_ELIGIBILITY:
        return position in FLEX_ELIGIBILITY[s]
    if s in DEFENSIVE_SLOTS:
        return position in ("DEF", "DST", "D")
    return s == position


def expand_slots(starting_slots: dict[str, int]) -> list[str]:
    """{"RB":2,"W/R/T":1} -> ["RB","RB","W/R/T"]"""
    out: list[str] = []
    for slot, count in starting_slots.items():
        if slot.upper() in BENCH_SLOTS:
            continue
        out.extend([slot] * int(count))
    return out


#: The caller's player type, preserved through the solver.
P = TypeVar("P")


@dataclass
class LineupSlot(Generic[P]):
    slot: str
    player: P | None
    points: float


@dataclass
class Lineup(Generic[P]):
    slots: list[LineupSlot[P]]
    total: float
    bench: list[P]

    @property
    def is_complete(self) -> bool:
        return all(s.player is not None for s in self.slots)

    @property
    def empty_slots(self) -> list[str]:
        return [s.slot for s in self.slots if s.player is None]


def best_lineup(
    players: Sequence[P],
    starting_slots: dict[str, int],
    *,
    points_of: Callable[[P], float] = lambda p: getattr(p, "points", 0.0) or 0.0,
    position_of: Callable[[P], str] = lambda p: getattr(p, "position", "") or "",
    eligible_of: Callable[[P], Iterable[str]] | None = None,
) -> Lineup[P]:
    """Highest-scoring legal lineup.

    `eligible_of` optionally supplies multi-position eligibility (Yahoo lets a
    player qualify at more than one position); it defaults to the primary one.
    """
    slots = expand_slots(starting_slots)
    n_slots = len(slots)
    if n_slots == 0:
        return Lineup([], 0.0, list(players))

    # Only the best few players per position can ever matter; trimming keeps the
    # DP small when handed a full free-agent pool.
    candidates = sorted(players, key=points_of, reverse=True)[: max(24, n_slots * 4)]

    def positions_for(player: P) -> set[str]:
        if eligible_of is not None:
            got = {str(p).upper() for p in eligible_of(player) if p}
            if got:
                return got
        return {str(position_of(player)).upper()}

    eligibility: list[int] = []
    for player in candidates:
        mask = 0
        player_positions = positions_for(player)
        for i, slot in enumerate(slots):
            if any(slot_accepts(slot, pos) for pos in player_positions):
                mask |= 1 << i
        eligibility.append(mask)

    full = (1 << n_slots) - 1
    NEG = float("-inf")
    # dp[mask] = (best total, back-pointer) after considering players so far.
    dp: dict[int, float] = {0: 0.0}
    choice: list[dict[int, tuple[int, int] | None]] = []

    for idx, player in enumerate(candidates):
        points = points_of(player)
        mask_options = eligibility[idx]
        new_dp = dict(dp)
        step: dict[int, tuple[int, int] | None] = {}
        for mask, value in dp.items():
            free = mask_options & ~mask
            bit = free & -free
            while bit:
                slot_index = bit.bit_length() - 1
                nxt = mask | bit
                candidate_value = value + points
                if candidate_value > new_dp.get(nxt, NEG):
                    new_dp[nxt] = candidate_value
                    step[nxt] = (mask, slot_index)
                free ^= bit
                bit = free & -free
        choice.append(step)
        dp = new_dp

    # Prefer a full lineup; otherwise the best partially-filled one.
    if full in dp:
        best_mask, best_value = full, dp[full]
    else:
        best_mask, best_value = max(dp.items(), key=lambda kv: (bin(kv[0]).count("1"), kv[1]))

    assignment: dict[int, P] = {}
    mask = best_mask
    for idx in range(len(candidates) - 1, -1, -1):
        step = choice[idx]
        entry = step.get(mask)
        if entry is not None:
            prev, slot_index = entry
            assignment[slot_index] = candidates[idx]
            mask = prev

    used = {id(p) for p in assignment.values()}
    lineup_slots = [
        LineupSlot(
            slot=slots[i],
            player=assignment.get(i),
            points=points_of(assignment[i]) if i in assignment else 0.0,
        )
        for i in range(n_slots)
    ]
    bench = [p for p in players if id(p) not in used]
    return Lineup(lineup_slots, round(best_value if best_value != NEG else 0.0, 2), bench)


def lineup_points(players: Sequence[object], starting_slots: dict[str, int], **kwargs) -> float:
    return best_lineup(players, starting_slots, **kwargs).total
