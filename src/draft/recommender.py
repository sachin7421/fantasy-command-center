"""Live draft recommendations (spec 5.2).

    score = VORP x need_multiplier x tier_urgency

- VORP is the master value number (src/vorp.py).
- need_multiplier reflects my unfilled starting slots versus bench depth, and
  suppresses K/DEF until the final rounds.
- tier_urgency boosts the last man in a tier when the survival model says he
  will not come back to me.

The output is deliberately explainable: every recommendation carries the reasons
that moved it, so a human can disagree on the spot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable, Sequence

from src.draft.survival import DraftPosition, survival_probability
from src.vorp import FLEX_ELIGIBILITY, Board, PlayerValue, split_slots


def build_tier_index(
    players: Sequence[PlayerValue],
) -> dict[tuple[str, int], list[PlayerValue]]:
    """Group available players by (position, tier) once, for fast tier lookups."""
    index: dict[tuple[str, int], list[PlayerValue]] = {}
    for p in players:
        index.setdefault((p.position, p.tier), []).append(p)
    return index


@dataclass
class RosterState:
    """My roster so far, and what it still needs."""

    starting_slots: dict[str, int]
    players: list[PlayerValue] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.players:
            out[p.position] = out.get(p.position, 0) + 1
        return out

    def unfilled(self) -> dict[str, float]:
        """Starting slots still open, with flex spread over eligible positions.

        Dedicated slots are filled greedily by the best player at that position;
        whatever is left over competes for the flex.
        """
        dedicated, flex = split_slots(self.starting_slots)
        counts = self.counts()
        leftovers: dict[str, int] = {}
        unfilled: dict[str, float] = {}

        for pos, needed in dedicated.items():
            have = counts.get(pos, 0)
            unfilled[pos] = max(0, needed - have)
            leftovers[pos] = max(0, have - needed)

        for slot, count in flex.items():
            eligible = FLEX_ELIGIBILITY[slot]
            spare = sum(leftovers.get(p, 0) for p in eligible)
            open_flex = max(0, count - spare)
            if open_flex:
                # A flex need is shared across the positions that can fill it.
                for pos in eligible:
                    if pos in ("K", "DEF"):
                        continue
                    unfilled[pos] = unfilled.get(pos, 0.0) + open_flex / len(
                        [e for e in eligible if e not in ("K", "DEF")]
                    )
        return unfilled

    def starters_needed(self) -> int:
        return int(round(sum(self.unfilled().values())))

    def bye_weeks(self) -> dict[int, list[PlayerValue]]:
        out: dict[int, list[PlayerValue]] = {}
        for p in self.players:
            if p.bye_week:
                out.setdefault(p.bye_week, []).append(p)
        return out


@dataclass
class Recommendation:
    player: PlayerValue
    score: float
    vorp: float
    need_multiplier: float
    tier_urgency: float
    survival: float | None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DraftRecommender:
    def __init__(
        self,
        board: Board,
        position: DraftPosition,
        *,
        need_weight: float = 0.35,
        defer_positions: Iterable[str] = ("K", "DEF"),
        defer_until_round: int = 13,
        bye_stack_threshold: int = 3,
        te_flex_credit: int = 0,
    ):
        self.board = board
        self.position = position
        self.need_weight = need_weight
        self.defer_positions = set(defer_positions)
        self.defer_until_round = defer_until_round
        self.bye_stack_threshold = bye_stack_threshold
        self.te_flex_credit = te_flex_credit

    # -- components ----------------------------------------------------------

    def startable_count(self, pos: str) -> int:
        """How many of this position could realistically be in a starting lineup.

        Flex capacity is only credited where the position actually competes for
        it. A tight end can legally fill a W/R/T, but with running backs and
        receivers on the roster the flex goes to one of them essentially every
        week - so crediting TE with flex spots inflates how many are worth
        rostering and burns bench slots on streamable backups. TE is therefore
        credited with its dedicated slots only. In a superflex league
        quarterbacks genuinely do claim the flex, so they get the full capacity.

        `te_flex_credit` in config raises this for leagues that really do start
        two tight ends.
        """
        dedicated, flex = split_slots(self.board.starting_slots)
        slots = dedicated.get(pos, 0)
        flex_capacity = sum(
            count for slot, count in flex.items() if pos in FLEX_ELIGIBILITY[slot]
        )
        if pos in ("K", "DEF"):
            return max(1, slots)
        if pos == "TE":
            return max(1, slots) + min(flex_capacity, self.te_flex_credit)
        if pos == "QB":
            return max(1, slots) + flex_capacity
        return slots + flex_capacity

    def position_cap(self, pos: str) -> int:
        """Most players worth rostering at a position.

        Without this the late rounds stack whatever has the least-negative VORP,
        which in practice means four defenses or four tight ends. A second DEF
        cannot start and cannot be flexed, so its real value is zero.
        """
        if pos in ("K", "DEF"):
            # Streaming a second one is a fringe strategy; one is the rule.
            return self.startable_count(pos)
        if pos in ("QB", "TE"):
            # What can start, plus a single backup.
            return self.startable_count(pos) + 1
        return 99  # RB/WR depth is always worth something

    def need_multiplier(self, pos: str, roster: RosterState, round_number: int) -> float:
        """Weight a position by how badly my starting lineup still needs it."""
        # Kickers and defenses are worth almost nothing early: their VORP spread
        # is tiny and they are freely available. Suppress, then force late.
        if pos in self.defer_positions:
            # Deferring is a timing preference, never a decision to go without.
            # A ten-round draft with defer_until_round=12 suppressed defence for
            # its entire length and ended with that slot empty - about 105 points
            # forfeited, which was the whole of the recommender's edge over
            # drafting by ADP. The naive baseline already had this rule; the
            # recommender did not.
            picks_left = max(0, self.position.rounds - round_number + 1)
            unfilled_now = roster.unfilled()
            still_needed = int(round(sum(unfilled_now.values())))
            non_deferred = int(round(sum(
                v for k, v in unfilled_now.items() if k not in self.defer_positions
            )))
            # Two conditions, and the second matters as much as the first:
            #   the slack is gone   - no pick left to spend elsewhere first
            #   and it is affordable - enough picks remain to cover the more
            #                          valuable slots too
            # Without the second, a draft too short to fill the lineup at all
            # (five rounds, nine starters) burns a top-60 pick on a defence
            # worth nine points over replacement.
            forced = (
                unfilled_now.get(pos, 0) > 0
                and picks_left <= still_needed
                and picks_left > non_deferred
            )
            if round_number < self.defer_until_round and not forced:
                return 0.05
            remaining = self.position.rounds - round_number
            urgency = 1.0 if roster.counts().get(pos, 0) == 0 else 0.2
            return urgency * (1.0 + max(0.0, (3 - remaining)) * 0.5)

        # Surplus check: could another one of these ever crack the lineup?
        # VORP measures a player against replacement *as a starter*, so a fourth
        # tight end still scores well above TE12 - yet he can never start, and a
        # comparable one is free on waivers. Value him as bench insurance.
        if roster.counts().get(pos, 0) >= max(1, self.startable_count(pos)):
            # Running back and receiver depth still pays: they carry the flex,
            # and injuries at those positions are frequent. Quarterback and tight
            # end are streamable, so a spare is worth very little.
            return 0.5 if pos in ("RB", "WR") else 0.2

        unfilled = roster.unfilled()
        open_slots = unfilled.get(pos, 0.0)
        total_open = sum(v for k, v in unfilled.items() if k not in self.defer_positions)

        if total_open <= 0:
            # Starters are set; this is bench/upside value only.
            return 1.0 - self.need_weight * 0.5

        share = open_slots / total_open if total_open else 0.0
        even_share = 1.0 / max(1, len([k for k in unfilled if k not in self.defer_positions]))
        # Above an even share => this position is the gap; below => already deep.
        relative = (share - even_share) / max(even_share, 1e-6)
        return max(0.4, 1.0 + self.need_weight * relative)

    def tier_urgency(
        self,
        player: PlayerValue,
        available: Sequence[PlayerValue],
        next_pick: int | None,
        tier_index: dict[tuple[str, int], list[PlayerValue]] | None = None,
        current_pick: int | None = None,
    ) -> tuple[float, float | None, list[str]]:
        """Boost the last man in a tier when he will not survive the wait.

        `tier_index` is a (position, tier) -> players map built once per pick;
        without it this rescans the pool for every candidate, which is O(n^2).
        """
        reasons: list[str] = []
        survival = None
        if next_pick is None:
            return 1.0, None, reasons

        # Conditioned on him still being here: a player who has already fallen
        # past his ADP is far likelier to keep falling than the unconditional
        # curve says, and fallers are where the value is.
        survival = survival_probability(
            player.adp, next_pick, player.adp_stdev, current_pick=current_pick
        )

        if tier_index is None:
            tier_index = build_tier_index(available)
        same_tier = tier_index.get((player.position, player.tier), [])
        next_tier = tier_index.get((player.position, player.tier + 1), [])

        # The size of the drop to the next tier, measured against the BOARD's
        # scale rather than against the player's own value.
        #
        # Dividing by `player.vorp` made urgency scale inversely with quality:
        # the same 10-point drop is 13% of an elite quarterback's VORP and 70%
        # of a mediocre one's, so the worse player got the bigger boost. Across
        # three mock drafts the recommender took QB8, QB9 and QB11 and never an
        # elite quarterback - about four points a week conceded to an artifact
        # rather than to a strategy.
        #
        # The denominator is now the best available player's VORP, which is the
        # same for every candidate at a given pick, so a cliff is comparable
        # across positions and a bigger drop always means more urgency.
        cliff = 0.0
        if next_tier:
            best_next = max(next_tier, key=lambda p: p.vorp)
            scale = max(
                (p.vorp for p in available if p.vorp > 0), default=0.0
            )
            if scale > 0 and player.vorp > best_next.vorp:
                cliff = max(0.0, (player.vorp - best_next.vorp) / scale)

        remaining_in_tier = len(same_tier)
        if remaining_in_tier <= 2 and cliff > 0:
            reasons.append(
                f"last {remaining_in_tier} in {player.position} tier {player.tier}; "
                f"next tier drops {cliff:.0%}"
            )

        # Urgency rises with both the size of the cliff and the chance he is gone.
        scarcity_pressure = cliff * (1.0 - survival)
        tier_thinness = 1.0 / max(1, remaining_in_tier)
        urgency = 1.0 + scarcity_pressure * (0.5 + 0.5 * tier_thinness)

        if survival < 0.35:
            reasons.append(f"{survival:.0%} chance he lasts to pick {next_pick}")
        elif survival > 0.85 and player.vorp > 0:
            reasons.append(f"likely available at {next_pick} ({survival:.0%}) - can wait")

        return urgency, survival, reasons

    # -- main entry point ----------------------------------------------------

    @staticmethod
    def _score_for(vorp: float, need: float, urgency: float) -> float:
        """Rank a player. Must be monotone increasing in `vorp` at any `need`.

        VORP can be negative, and a negative multiplied by a bonus points the
        wrong way. The old expression `vorp * (2.0 - need)` handled that below
        need=2 and silently inverted above it - and nothing caps `need`, which
        the deferred-position path drives to 2.5 in the final round. A kicker
        at VORP -30 then scored +15 and ranked FIRST, ahead of one at -5: the
        recommender preferred the worse player in the exact round it forces you
        to draft one.

        Dividing instead of multiplying below replacement keeps need meaningful
        (a needed position still sorts above an unneeded one) while making it
        impossible for need to reorder two players against their value.
        """
        if vorp > 0:
            return vorp * need * urgency
        return vorp / max(need, 1e-6) * urgency

    def recommend(
        self,
        drafted: set[str],
        roster: RosterState,
        current_pick: int,
        top_n: int = 10,
    ) -> list[Recommendation]:
        available = self.board.available(drafted)
        round_number = self.position.current_round(current_pick)
        next_pick = self.position.next_pick_after(current_pick)

        bye_counts = {week: len(players) for week, players in roster.bye_weeks().items()}
        tier_index = build_tier_index(available)
        results: list[Recommendation] = []

        counts = roster.counts()
        for player in available:
            if player.vorp <= -50:  # deep bench noise, not worth ranking
                continue
            if counts.get(player.position, 0) >= self.position_cap(player.position):
                continue  # another one of these cannot start; it is dead weight
            need = self.need_multiplier(player.position, roster, round_number)
            urgency, survival, tier_reasons = self.tier_urgency(
                player, available, next_pick, tier_index, current_pick=current_pick
            )

            # VORP can be negative, and a negative multiplied by a bonus points
            # the wrong way. The old expression `base * (2.0 - need)` handled
            # that for need < 2, then silently inverted again above it: nothing
            # caps `need`, and the deferred-position path reaches 2.5 in the
            # final round. A kicker at VORP -30 with need 2.5 scored +15 and
            # ranked FIRST, ahead of a kicker at -5 - the recommender preferred
            # the worse player, in the exact round it forces you to draft one.
            #
            # Shifting into a strictly positive space instead keeps the ordering
            # monotone in VORP for every value of `need`: a higher VORP always
            # scores higher at the same need, and need still ranks positions.
            score = self._score_for(player.vorp, need, urgency)

            reasons = list(tier_reasons)
            warnings: list[str] = []

            delta = player.adp_delta
            if delta is not None and delta >= 12:
                reasons.append(f"value falling to you: ADP {player.adp:.0f}, board rank {player.overall_rank}")
            elif delta is not None and delta <= -12:
                warnings.append(f"reach vs ADP {player.adp:.0f} (board rank {player.overall_rank})")

            if need > 1.05:
                reasons.append(f"fills a starting {player.position} need")
            elif need < 0.6 and player.position not in self.defer_positions:
                reasons.append(f"{player.position} already deep")

            if player.injury_status and player.injury_status not in ("Healthy",):
                warnings.append(f"injury: {player.injury_status}")

            # Last season, relative to what his own opportunity implied. The
            # market prices players on what they scored, so the gap between
            # that and what their usage supported is where value hides.
            if player.prior_verdict == "inflated":
                warnings.append(
                    f"last season ran hot ({player.prior_z:+.1f} sd for a "
                    f"{player.position}); most of that does not repeat"
                )
            elif player.prior_verdict == "deflated":
                reasons.append(
                    f"last season ran cold ({player.prior_z:+.1f} sd for a "
                    f"{player.position}); his usage was better than his points"
                )

            if player.bye_week:
                stacked = bye_counts.get(player.bye_week, 0)
                if stacked + 1 >= self.bye_stack_threshold:
                    warnings.append(
                        f"bye stack: would be {stacked + 1} starters on week {player.bye_week}"
                    )

            results.append(
                Recommendation(
                    player=player,
                    score=round(score, 2),
                    vorp=player.vorp,
                    need_multiplier=round(need, 3),
                    tier_urgency=round(urgency, 3),
                    survival=survival,
                    reasons=reasons,
                    warnings=warnings,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)

        # Two filters run above - below-replacement noise, and positions this
        # roster can no longer start - and each is right on its own. Together
        # they can remove EVERY remaining player, which happens in the late
        # rounds of a thin board. The caller then gets [], which renders as an
        # empty panel: it reads as "no good options" rather than "this is
        # broken", and on a 90-second clock nobody investigates a blank panel.
        #
        # There is always a best remaining player, so say who it is and be
        # honest about why he was filtered out. Silence is the one answer this
        # is not allowed to give.
        if not results and available:
            fallback = max(available, key=lambda p: p.vorp)
            results = [
                Recommendation(
                    player=fallback,
                    score=round(self._score_for(fallback.vorp, 1.0, 0.0), 2),
                    vorp=fallback.vorp,
                    need_multiplier=1.0,
                    tier_urgency=0.0,
                    survival=None,
                    reasons=["best of what is left"],
                    warnings=[
                        "everyone left is below replacement or plays a position "
                        "this roster cannot start another of"
                    ],
                )
            ]

        return results[:top_n]

    # -- helpers for the UI --------------------------------------------------

    def best_available_by_position(
        self, drafted: set[str], per_position: int = 5
    ) -> dict[str, list[PlayerValue]]:
        out: dict[str, list[PlayerValue]] = {}
        for player in self.board.available(drafted):
            bucket = out.setdefault(player.position, [])
            if len(bucket) < per_position:
                bucket.append(player)
        return out
