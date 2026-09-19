"""Timing the phases of a long command, so the slow one can be named.

`fcc sync` overran the 20-minute job timeout on 18 and 19 Sep and cancelled the
morning run with it. Its output named ten phases and timed none of them, so
the diagnosis was "somewhere in sync" and the only way to narrow it was to
watch a 15-minute run go past.

Deliberately tiny: a dict of seconds and one line of output. Anything that
needs more than this belongs in a profiler, not in a scheduled job.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class PhaseTimer:
    """Accumulates seconds per named phase.

    `clock` is injectable so tests do not sleep. It defaults to
    `time.monotonic` rather than `time.time`: wall time can jump when the
    machine's clock is corrected mid-run, which would print a negative phase.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.elapsed: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time one phase. An exception is timed too, then re-raised.

        A phase that fails is exactly the one whose cost is worth knowing - a
        source that hangs and then raises is the shape of this whole problem.
        """
        started = self.clock()
        try:
            yield
        finally:
            taken = self.clock() - started
            self.elapsed[name] = round(self.elapsed.get(name, 0.0) + taken, 4)

    def summary(self) -> str:
        """One line, worst phase first, with the total."""
        if not self.elapsed:
            return "nothing timed"
        ranked = sorted(self.elapsed.items(), key=lambda kv: kv[1], reverse=True)
        parts = ", ".join(f"{name} {seconds:.1f}s" for name, seconds in ranked)
        return f"{parts} - total {sum(self.elapsed.values()):.1f}s"
