"""Will he last until my next pick? (spec 5.1)

The single most useful number during a draft is not "who is best" but "who will
still be here next time". If a player is very likely to survive, you can take
the scarcer player now and still get him later.

Model: a player's actual draft slot T is treated as Normal(ADP, sigma). Then
    P(available at pick N) = P(T >= N) = 1 - Phi((N - ADP) / sigma)

sigma comes from the disagreement between ADP variants where we have it, and
otherwise from a fitted curve: uncertainty grows with ADP, because pick 3 is far
more predictable than pick 103.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: Fitted sigma floor/growth. Early picks are tight; late picks are noisy.
SIGMA_BASE = 3.0
SIGMA_GROWTH = 0.28
SIGMA_MIN = 2.0
SIGMA_MAX = 45.0

#: Players with no ADP at all are treated as effectively undrafted.
UNDRAFTED_SURVIVAL = 0.97


def normal_cdf(x: float) -> float:
    """Phi(x) via the error function - no scipy dependency needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def default_sigma(adp: float) -> float:
    """Uncertainty in a player's draft slot, as a function of where he goes."""
    return max(SIGMA_MIN, min(SIGMA_MAX, SIGMA_BASE + SIGMA_GROWTH * adp))


def sigma_for(adp: float | None, adp_stdev: float | None) -> float:
    if adp is None:
        return SIGMA_MAX
    if adp_stdev and adp_stdev > 0:
        # Observed spread across scoring formats understates true draft-room
        # variance, so blend it with the fitted curve rather than trusting it.
        return max(SIGMA_MIN, min(SIGMA_MAX, 0.5 * adp_stdev + 0.5 * default_sigma(adp)))
    return default_sigma(adp)


def survival_probability(
    adp: float | None,
    pick_number: int,
    adp_stdev: float | None = None,
    already_drafted: bool = False,
    current_pick: int | None = None,
) -> float:
    """P(still on the board at `pick_number`, GIVEN he is on it now.

    The conditioning is the point. Unconditionally this is `S(next)`, the
    chance a player lasts that long measured from before the draft started -
    but during a draft you can see that he is still there, and the useful
    quantity is `S(next) / S(current)`.

    The difference is largest for exactly the players who matter most, the ones
    falling past their ADP:

        ADP 35, still available at pick 50, my next pick is 62
            unconditional  0.017      conditional  0.145      8.3x

    Treating a faller as already dead is what an unconditional model does, and
    a faller is the best value on the board.
    """
    if already_drafted:
        return 0.0
    if adp is None:
        return UNDRAFTED_SURVIVAL

    sigma = sigma_for(adp, adp_stdev)

    def survives_to(pick: int) -> float:
        return max(0.0, min(1.0, 1.0 - normal_cdf((pick - adp) / sigma)))

    unconditional = survives_to(pick_number)
    if current_pick is None or current_pick >= pick_number:
        return unconditional

    # He is observably still available at `current_pick`, so renormalise.
    so_far = survives_to(current_pick)
    if so_far <= 1e-9:
        # The model says he should already be gone and he is not, so it has
        # nothing useful to say. Fall back to the undrafted prior rather than
        # dividing by ~0 and claiming certainty.
        return UNDRAFTED_SURVIVAL
    return max(0.0, min(1.0, unconditional / so_far))


@dataclass
class DraftPosition:
    """Where I sit in the draft order, and when I pick next."""

    num_teams: int
    draft_slot: int           # 1-based
    snake: bool = True
    rounds: int = 16

    def picks(self) -> list[int]:
        """Every overall pick number that belongs to me."""
        out = []
        for rnd in range(1, self.rounds + 1):
            if self.snake and rnd % 2 == 0:
                slot = self.num_teams - self.draft_slot + 1
            else:
                slot = self.draft_slot
            out.append((rnd - 1) * self.num_teams + slot)
        return out

    def current_round(self, overall_pick: int) -> int:
        return (max(1, overall_pick) - 1) // self.num_teams + 1

    def next_pick_after(self, overall_pick: int) -> int | None:
        """My first pick strictly after `overall_pick`."""
        for p in self.picks():
            if p > overall_pick:
                return p
        return None

    def picks_until_next(self, overall_pick: int) -> int | None:
        nxt = self.next_pick_after(overall_pick)
        return None if nxt is None else nxt - overall_pick


def wait_cost(
    candidates: list[tuple[str, float, float | None, float | None]],
    next_pick: int,
) -> dict[str, float]:
    """Expected value lost per player by waiting until `next_pick`.

    `candidates` is (player_key, vorp, adp, adp_stdev). The cost of waiting on a
    player is his value times the chance he is gone.
    """
    out: dict[str, float] = {}
    for key, value, adp, stdev in candidates:
        p = survival_probability(adp, next_pick, stdev)
        out[key] = value * (1.0 - p)
    return out
