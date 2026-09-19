"""Where a long job actually spends its time.

`fcc sync` overran the 20-minute job timeout on 18 and 19 Sep and took the
whole morning run with it. The output named ten phases and timed none of them,
so "somewhere in sync" was the entire diagnosis, and the only way to learn more
was to sit and watch a 15-minute run.

A phase that is never timed is a phase nobody can make faster.
"""
from __future__ import annotations

import pytest

from src.timing import PhaseTimer


def test_each_phase_records_its_own_elapsed_time():
    clock = iter([0.0, 2.0, 2.0, 5.5]).__next__
    timer = PhaseTimer(clock=clock)
    with timer.phase("players"):
        pass
    with timer.phase("adp"):
        pass
    assert timer.elapsed == {"players": 2.0, "adp": 3.5}


def test_the_summary_leads_with_the_worst_phase():
    """The point is to name the thing to fix, so it goes first."""
    clock = iter([0.0, 1.0, 1.0, 61.0, 61.0, 61.5]).__next__
    timer = PhaseTimer(clock=clock)
    for name in ("players", "adp", "byes"):
        with timer.phase(name):
            pass
    assert timer.summary() == "adp 60.0s, players 1.0s, byes 0.5s - total 61.5s"


def test_a_phase_that_raises_is_still_timed_and_the_error_still_travels():
    """A phase that blows up is exactly the one worth knowing the cost of."""
    clock = iter([0.0, 4.0]).__next__
    timer = PhaseTimer(clock=clock)
    with pytest.raises(ValueError, match="sleeper"), timer.phase("adp"):
        raise ValueError("sleeper is down")
    assert timer.elapsed == {"adp": 4.0}


def test_the_same_phase_twice_accumulates_rather_than_overwrites():
    """Weekly projections run once per week; each pass is part of one cost."""
    clock = iter([0.0, 1.5, 10.0, 12.5]).__next__
    timer = PhaseTimer(clock=clock)
    for _ in range(2):
        with timer.phase("weekly proj"):
            pass
    assert timer.elapsed == {"weekly proj": 4.0}


def test_nothing_timed_says_so_rather_than_printing_an_empty_line():
    assert PhaseTimer().summary() == "nothing timed"


def test_the_real_clock_is_monotonic_not_wall_time():
    """Wall time moves when the machine's clock is corrected mid-run."""
    import time

    assert PhaseTimer().clock is time.monotonic
