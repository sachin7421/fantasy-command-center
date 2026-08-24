"""Lineup solver tests, including the cases where greedy filling goes wrong."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.lineup_solver import best_lineup, expand_slots, slot_accepts


@dataclass
class P:
    name: str
    position: str
    points: float


SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1}


def names(lineup):
    return {s.slot: (s.player.name if s.player else None) for s in lineup.slots}


def test_slot_eligibility():
    assert slot_accepts("RB", "RB")
    assert not slot_accepts("RB", "WR")
    assert slot_accepts("W/R/T", "TE")
    assert not slot_accepts("W/R/T", "QB")
    assert slot_accepts("Q/W/R/T", "QB")
    assert not slot_accepts("BN", "RB")


def test_expand_slots_skips_bench():
    slots = expand_slots({"RB": 2, "W/R/T": 1, "BN": 6, "IR": 2})
    assert sorted(slots) == ["RB", "RB", "W/R/T"]


def test_picks_highest_scoring_lineup():
    roster = [
        P("QB1", "QB", 20), P("RB1", "RB", 18), P("RB2", "RB", 14), P("RB3", "RB", 9),
        P("WR1", "WR", 16), P("WR2", "WR", 12), P("TE1", "TE", 8),
        P("K1", "K", 7), P("D1", "DEF", 6),
    ]
    lineup = best_lineup(roster, SLOTS)
    assert lineup.is_complete
    # RB3 (9) beats nobody else for the flex; total is everything but nothing left over.
    assert lineup.total == pytest.approx(20 + 18 + 14 + 16 + 12 + 8 + 9 + 7 + 6)


def test_flex_does_not_strand_a_position_locked_player():
    """The case greedy gets wrong.

    Greedy fills FLEX with the best leftover (RB3, 15) and then has no WR slot
    left for WR3 - but the roster only has two WRs, so a WR slot would sit empty.
    The exact solver must keep both WR slots filled.
    """
    roster = [
        P("QB1", "QB", 20),
        P("RB1", "RB", 18), P("RB2", "RB", 17), P("RB3", "RB", 15),
        P("WR1", "WR", 16), P("WR2", "WR", 10),
        P("TE1", "TE", 9), P("K1", "K", 7), P("D1", "DEF", 6),
    ]
    lineup = best_lineup(roster, SLOTS)
    assert lineup.is_complete
    filled = names(lineup)
    assert filled["QB"] == "QB1"
    assert filled["W/R/T"] == "RB3"          # flex correctly takes the third RB
    assert lineup.total == pytest.approx(20 + 18 + 17 + 16 + 10 + 9 + 15 + 7 + 6)


def test_flex_prefers_the_globally_better_arrangement():
    """A strong WR3 should take the flex over a weaker RB3."""
    roster = [
        P("QB1", "QB", 20),
        P("RB1", "RB", 18), P("RB2", "RB", 12), P("RB3", "RB", 6),
        P("WR1", "WR", 17), P("WR2", "WR", 15), P("WR3", "WR", 14),
        P("TE1", "TE", 9), P("K1", "K", 7), P("D1", "DEF", 6),
    ]
    lineup = best_lineup(roster, SLOTS)
    assert names(lineup)["W/R/T"] == "WR3"
    assert lineup.total == pytest.approx(20 + 18 + 12 + 17 + 15 + 9 + 14 + 7 + 6)


def test_incomplete_roster_reports_empty_slots():
    roster = [P("QB1", "QB", 20), P("RB1", "RB", 18)]
    lineup = best_lineup(roster, SLOTS)
    assert not lineup.is_complete
    assert "K" in lineup.empty_slots and "DEF" in lineup.empty_slots
    assert lineup.total == pytest.approx(38)


def test_bench_is_everyone_not_starting():
    roster = [
        P("QB1", "QB", 20), P("QB2", "QB", 19),
        P("RB1", "RB", 18), P("RB2", "RB", 14), P("WR1", "WR", 16), P("WR2", "WR", 12),
        P("TE1", "TE", 8), P("K1", "K", 7), P("D1", "DEF", 6),
    ]
    lineup = best_lineup(roster, SLOTS)
    bench_names = {p.name for p in lineup.bench}
    assert "QB2" in bench_names
    # The flex should absorb nobody illegal: only WR/RB/TE are eligible.
    assert names(lineup)["W/R/T"] is None or names(lineup)["W/R/T"] not in ("QB2",)


def test_superflex_can_start_a_second_quarterback():
    slots = {"QB": 1, "RB": 1, "WR": 1, "Q/W/R/T": 1}
    roster = [
        P("QB1", "QB", 25), P("QB2", "QB", 22),
        P("RB1", "RB", 14), P("WR1", "WR", 13), P("WR2", "WR", 11),
    ]
    lineup = best_lineup(roster, slots)
    assert names(lineup)["Q/W/R/T"] == "QB2"
    assert lineup.total == pytest.approx(25 + 22 + 14 + 13)


def test_multi_position_eligibility_is_respected():
    """A player Yahoo lists at RB and WR can fill either slot."""
    roster = [
        P("Swiss", "RB", 15), P("RB1", "RB", 14), P("WR1", "WR", 13),
    ]
    slots = {"RB": 1, "WR": 2}
    lineup = best_lineup(
        roster, slots, eligible_of=lambda p: ["RB", "WR"] if p.name == "Swiss" else [p.position]
    )
    # Swiss must slide to WR so both RB and WR slots fill.
    assert lineup.is_complete
    assert lineup.total == pytest.approx(15 + 14 + 13)
