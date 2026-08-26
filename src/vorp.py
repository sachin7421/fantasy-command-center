"""Value over replacement, tiers and positional scarcity (spec 5.1).

Raw projected points are a trap: a QB out-scores every RB, yet QBs go late
because the *replacement* QB also scores a lot. Value is what a player adds over
the player you could have had for free at that position, which is what this
module computes.

Everything is derived from the league's real roster settings - starting slots,
flex slots and team count all come from Yahoo (spec 2.3).
"""
from __future__ import annotations

import logging

from dataclasses import dataclass, field
from collections.abc import Iterable, Sequence
from src.storage import Database

# Which real positions each Yahoo flex slot can absorb.
FLEX_ELIGIBILITY: dict[str, set[str]] = {
    "W/R": {"WR", "RB"},
    "W/T": {"WR", "TE"},
    "R/T": {"RB", "TE"},
    "W/R/T": {"WR", "RB", "TE"},
    "Q/W/R/T": {"QB", "WR", "RB", "TE"},
    "W/R/T/Q": {"QB", "WR", "RB", "TE"},
    "OP": {"QB", "WR", "RB", "TE"},
    "SUPERFLEX": {"QB", "WR", "RB", "TE"},
}

DEFENSIVE_SLOTS = {"DEF", "DST", "D"}
BENCH_SLOTS = {"BN", "IR", "IR+", "NA"}

REAL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


log = logging.getLogger(__name__)


@dataclass
class PlayerValue:
    player_key: str
    name: str
    position: str
    team: str
    points: float
    vorp: float = 0.0
    tier: int = 0
    position_rank: int = 0
    overall_rank: int = 0
    adp: float | None = None
    adp_stdev: float | None = None
    bye_week: int | None = None
    injury_status: str | None = None
    floor: float | None = None
    ceiling: float | None = None
    #: What last season says about this one: "inflated", "deflated", or None.
    #: A player whose prior year beat league-average efficiency by more than
    #: the position normally does is priced on a season most of which will not
    #: repeat - see src/analytics/priors.py for the measurement.
    prior_verdict: str | None = None
    prior_z: float | None = None
    prior_note: str | None = None
    #: Points the prior-season adjustment removed (negative) or added.
    prior_adjustment: float = 0.0

    @property
    def adp_delta(self) -> float | None:
        """Positive means he is still on the board later than ADP expects."""
        if self.adp is None or not self.overall_rank:
            return None
        return self.adp - self.overall_rank


@dataclass
class ReplacementLevel:
    position: str
    rank: int                 # league-wide rank that defines replacement
    points: float
    dedicated_starters: int
    flex_share: float
    #: True when the position has fewer projected players than starting slots,
    #: so there is no genuine replacement to measure against and every VORP at
    #: that position is optimistic.
    scarce: bool = False


@dataclass
class Board:
    """A fully valued draft board."""

    players: list[PlayerValue] = field(default_factory=list)
    replacement: dict[str, ReplacementLevel] = field(default_factory=dict)
    tiers: dict[str, list[list[PlayerValue]]] = field(default_factory=dict)
    scarcity: dict[str, float] = field(default_factory=dict)
    starting_slots: dict[str, int] = field(default_factory=dict)
    num_teams: int = 12

    def by_position(self, position: str) -> list[PlayerValue]:
        return [p for p in self.players if p.position == position]

    def available(self, drafted: set[str]) -> list[PlayerValue]:
        return [p for p in self.players if p.player_key not in drafted]

    def get(self, player_key: str) -> PlayerValue | None:
        for p in self.players:
            if p.player_key == player_key:
                return p
        return None


# --- roster demand -----------------------------------------------------------

def split_slots(starting_slots: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Separate dedicated position slots from flex slots."""
    dedicated: dict[str, int] = {}
    flex: dict[str, int] = {}
    for slot, count in starting_slots.items():
        if slot in BENCH_SLOTS or count <= 0:
            continue
        normalized = slot.upper()
        if normalized in FLEX_ELIGIBILITY:
            flex[normalized] = flex.get(normalized, 0) + count
        elif normalized in DEFENSIVE_SLOTS:
            dedicated["DEF"] = dedicated.get("DEF", 0) + count
        else:
            dedicated[normalized] = dedicated.get(normalized, 0) + count
    return dedicated, flex


def positions_from_slots(starting_slots: dict[str, int]) -> list[str]:
    """The positions this league actually starts.

    A league with no K slot should not have kickers on its board at all, and a
    superflex league must include QBs in flex competition.
    """
    dedicated, flex = split_slots(starting_slots)
    covered: set[str] = set(dedicated)
    for slot in flex:
        covered |= FLEX_ELIGIBILITY[slot]
    return [p for p in REAL_POSITIONS if p in covered]


def compute_replacement_levels(
    players_by_position: dict[str, list[PlayerValue]],
    starting_slots: dict[str, int],
    num_teams: int,
) -> dict[str, ReplacementLevel]:
    """Find the replacement-level player at each position.

    Dedicated slots are easy: 12 teams x 2 RB means RB24 is the last starter.
    Flex slots are the interesting part - we do not guess how a flex splits
    across positions, we *simulate* it: walk the best remaining players at every
    flex-eligible position and see which ones actually claim the flex spots.
    """
    dedicated, flex = split_slots(starting_slots)

    # Baseline: dedicated starters league-wide.
    baseline = {pos: num_teams * count for pos, count in dedicated.items()}
    for pos in players_by_position:
        baseline.setdefault(pos, 0)

    # Simulate the flex draft to learn each position's real share.
    flex_share: dict[str, float] = dict.fromkeys(baseline, 0.0)
    cursor = dict(baseline)
    for slot, count in flex.items():
        # Sorted, because FLEX_ELIGIBILITY holds sets and Python randomises
        # string hashing per process: on an exact points tie the flex spot went
        # to a different position from one run to the next, shifting every
        # replacement level and therefore the whole board. Rare with float
        # projections, but a numeric pipeline should not be nondeterministic.
        eligible = sorted(FLEX_ELIGIBILITY[slot])
        for _ in range(num_teams * count):
            best_pos, best_points = None, float("-inf")
            for pos in eligible:
                pool = players_by_position.get(pos) or []
                idx = cursor.get(pos, 0)
                if idx >= len(pool):
                    continue
                if pool[idx].points > best_points:
                    best_pos, best_points = pos, pool[idx].points
            if best_pos is None:
                break
            cursor[best_pos] = cursor.get(best_pos, 0) + 1
            flex_share[best_pos] = flex_share.get(best_pos, 0.0) + 1

    levels: dict[str, ReplacementLevel] = {}
    for pos, pool in players_by_position.items():
        if not pool:
            continue
        rank = int(baseline.get(pos, 0) + flex_share.get(pos, 0.0))
        # The replacement player is the *next* one after the last starter -
        # which is what the comment always said and the arithmetic never did.
        # `rank` counts starters, so the last starter is pool[rank - 1] and the
        # first man off the bench is pool[rank]. Taking the starter understated
        # every VORP by that position's own starter-N to starter-N+1 gap, and
        # since that gap is much steeper at RB and TE than at QB, it distorted
        # exactly the cross-position comparison this module exists to make.
        #
        # If a position has fewer players than starting slots, there IS no
        # replacement: it is not that the worst player in the pool is freely
        # available, it is that nothing is. Clamping to the last man handed
        # every one of them a large positive VORP.
        replacement_index = max(rank, 1)
        if replacement_index >= len(pool):
            scarce = True
            replacement_points = pool[-1].points
        else:
            scarce = False
            replacement_points = pool[replacement_index].points
        levels[pos] = ReplacementLevel(
            position=pos,
            rank=max(rank, 1) + 1,
            points=replacement_points,
            scarce=scarce,
            dedicated_starters=dedicated.get(pos, 0),
            flex_share=flex_share.get(pos, 0.0),
        )
    return levels


# --- tiers -------------------------------------------------------------------

#: A tier break needs a gap this many times the position's own typical step.
TIER_BREAK_MULTIPLE = 1.6
#: ...and at least this many players before another break can start.
MIN_TIER_SIZE = 2


def assign_tiers(
    players: Sequence[PlayerValue],
    gap_pct: float = 0.08,
    min_gap_points: float = 3.0,
    break_multiple: float = TIER_BREAK_MULTIPLE,
) -> list[list[PlayerValue]]:
    """Group a position into tiers at projection drop-offs (spec 5.1).

    A break is measured against the position's OWN median step, not against
    absolute thresholds. The absolute rule - a gap of at least 3.0 points AND
    at least 8% of the player's value - essentially never fired on smoothed
    season projections, where consecutive gaps run 2 to 5 points on totals of
    150 to 300. Every running back from the RB1 down to the RB30 came back as
    Tier 1, and every receiver likewise.

    That was not a cosmetic problem. Tiers exist to answer one question - "if I
    wait a round, do I fall off a cliff?" - and a board with one tier answers
    "no cliff, ever". It also silently disabled `tier_urgency` in the
    recommender, which is the only mechanism that would tell you to take the
    last elite tight end before the drop.

    The absolute floor is kept as a secondary guard so the deep end of a
    position does not shatter into noise tiers.
    """
    ordered = sorted(players, key=lambda p: p.points, reverse=True)
    if not ordered:
        return []

    gaps = [
        ordered[i].points - ordered[i + 1].points for i in range(len(ordered) - 1)
    ]
    positive = sorted(g for g in gaps if g > 0)
    typical = positive[len(positive) // 2] if positive else 0.0
    # Relative to the position's own step, with the old absolute rule as a
    # floor so a flat, low-scoring tail does not fragment.
    threshold = max(typical * break_multiple, min_gap_points * 0.5)

    tiers: list[list[PlayerValue]] = [[]]
    tier_number = 1
    for i, player in enumerate(ordered):
        player.tier = tier_number
        tiers[-1].append(player)
        if i + 1 >= len(ordered):
            break
        gap = gaps[i]
        big_enough = gap >= threshold
        # The original relative rule still forces a break on a genuine cliff
        # even where the position's steps are large.
        cliff = gap >= min_gap_points and (gap / (abs(player.points) or 1.0)) >= gap_pct
        if (big_enough or cliff) and len(tiers[-1]) >= MIN_TIER_SIZE:
            tier_number += 1
            tiers.append([])
    return [t for t in tiers if t]


def market_disagreement(
    players: Sequence[PlayerValue], depth: int = 60
) -> dict[str, dict[str, float]]:
    """Where this board systematically disagrees with the draft room, by position.

    Worth stating plainly because it is the board's biggest open question. On
    the 2026 pre-season data every one of the top 25 running backs ranks AHEAD
    of its half-PPR ADP and only 2 of 25 receivers do - a perfectly one-sided
    split, which is the signature of a bias rather than an edge.

    What it is NOT: a scoring-format mismatch. The ADP variant is selected from
    the league's own PPR value, and this league takes the half-PPR one.

    What it might be: the projection sources genuinely differ at running back
    (ESPN runs about 10% above Sleeper there and level everywhere else), or the
    market prices injury and role risk that a point estimate does not. Which of
    those is right cannot be settled from pre-season data alone - it needs a
    season of stored projections graded against what happened, which is what
    `fcc accuracy` accumulates.

    Until then this reports the disagreement rather than silently presenting one
    side as fact, so a human can apply judgment at the pick.
    """
    from statistics import median

    by_position: dict[str, list[float]] = {}
    for player in players[:depth]:
        if player.adp and player.overall_rank:
            by_position.setdefault(player.position, []).append(
                player.adp - player.overall_rank
            )

    out: dict[str, dict[str, float]] = {}
    for position, deltas in by_position.items():
        if len(deltas) < 5:
            continue
        ahead = sum(1 for d in deltas if d > 0)
        out[position] = {
            "n": float(len(deltas)),
            "median_delta": round(median(deltas), 1),
            "share_ahead_of_adp": round(ahead / len(deltas), 2),
        }
    return out


# --- scarcity ----------------------------------------------------------------

def scarcity_curve(
    players: Sequence[PlayerValue], horizon: int, num_teams: int
) -> float:
    """How fast value decays at a position, in points lost per pick waited.

    Measured over the next `horizon` players at the position - i.e. roughly the
    window before your next pick comes around. A high number means waiting is
    expensive and the position must be addressed now (spec 5.1).
    """
    ordered = sorted(players, key=lambda p: p.points, reverse=True)
    if len(ordered) < 2:
        return 0.0
    window = ordered[: max(2, min(horizon, len(ordered)))]
    drop = window[0].points - window[-1].points
    return drop / max(1, len(window) - 1)


# --- board assembly ----------------------------------------------------------

def _apply_prior_season(
    conn: Database,
    season: int,
    players_by_position: dict[str, list[PlayerValue]],
    strength: float,
) -> bool:
    """Returns True when any projection was actually moved."""
    """Attach prior-season regression flags, and optionally act on them.

    Flagging is always on and costs nothing; adjusting the projection is opt-in
    via `draft.prior_regression_strength`, because the projection sources have
    already priced in some of the same effect and applying it at full force
    would double-count. At strength 0 the board is unchanged and the flag is
    shown for you to judge.

    Silent no-op when last season's usage has not been synced, which is the
    normal state until `fcc sync-usage` has run.
    """
    try:
        from src.analytics.priors import draft_adjustment, flag_players

        flags = flag_players(conn, season - 1)
    except Exception:
        # The board must never fail for this - but it must not go quiet either.
        # Without the adjustment every value on the board changes, and a silent
        # return leaves no way to tell that from the adjustment finding nothing.
        log.warning(
            "prior-season adjustment unavailable; the board is built from raw "
            "projections only", exc_info=True,
        )
        return False
    if not flags:
        return False

    moved = False

    for pool in players_by_position.values():
        for player in pool:
            flag = flags.get(player.player_key)
            if flag is None or not flag.is_flagged:
                continue
            player.prior_verdict = flag.verdict
            player.prior_z = flag.z
            player.prior_note = flag.reasons[0] if flag.reasons else None
            if strength > 0:
                adjusted = draft_adjustment(player.points, flag, strength)
                if adjusted != player.points:
                    moved = True
                player.prior_adjustment = round(adjusted - player.points, 2)
                player.points = round(adjusted, 2)

    return moved


def build_board(
    conn: Database,
    season: int,
    starting_slots: dict[str, int],
    num_teams: int,
    *,
    week: int = 0,
    tier_gap_pct: float = 0.08,
    positions: Iterable[str] | None = None,
    limit_per_position: int | None = None,
    prior_strength: float = 0.0,
) -> Board:
    """Assemble the valued board from stored projections."""
    # Default to exactly the positions this league starts, so a no-kicker league
    # never shows kickers.
    if positions is None:
        positions = positions_from_slots(starting_slots) or list(REAL_POSITIONS)
    players_by_position: dict[str, list[PlayerValue]] = {}

    for pos in positions:
        rows = conn.execute(
            _BOARD_QUERY,
            {"season": season, "week": week, "position": pos},
        ).fetchall()
        pool = [
            PlayerValue(
                player_key=r["player_key"],
                name=r["full_name"],
                position=pos,
                team=r["team"] or "",
                points=float(r["points"] or 0.0),
                adp=r["adp"],
                adp_stdev=r["adp_stdev"],
                bye_week=r["bye_week"],
                injury_status=r["injury_status"],
                floor=r["floor"],
                ceiling=r["ceiling"],
            )
            for r in rows
        ]
        if limit_per_position:
            pool = pool[:limit_per_position]
        for i, p in enumerate(pool, 1):
            p.position_rank = i
        if pool:
            players_by_position[pos] = pool

    # What last season says. Attached before replacement levels are computed,
    # because an adjusted projection changes where replacement sits - and the
    # pools are re-sorted and re-ranked afterwards, because it also changes the
    # ORDER, which every index into these lists assumes.
    if _apply_prior_season(conn, season, players_by_position, prior_strength):
        for pool in players_by_position.values():
            pool.sort(key=lambda p: p.points, reverse=True)
            for i, player in enumerate(pool, 1):
                player.position_rank = i

    replacement = compute_replacement_levels(
        players_by_position, starting_slots, num_teams
    )

    all_players: list[PlayerValue] = []
    tiers: dict[str, list[list[PlayerValue]]] = {}
    scarcity: dict[str, float] = {}

    for pos, pool in players_by_position.items():
        level = replacement.get(pos)
        base = level.points if level else 0.0
        for p in pool:
            p.vorp = round(p.points - base, 2)
        tiers[pos] = assign_tiers(pool, tier_gap_pct)
        scarcity[pos] = round(scarcity_curve(pool, num_teams, num_teams), 3)
        all_players.extend(pool)

    all_players.sort(key=lambda p: p.vorp, reverse=True)
    for i, p in enumerate(all_players, 1):
        p.overall_rank = i

    return Board(
        players=all_players,
        replacement=replacement,
        tiers=tiers,
        scarcity=scarcity,
        starting_slots=starting_slots,
        num_teams=num_teams,
    )


_BOARD_QUERY = """
SELECT
    p.player_key,
    p.full_name,
    p.team,
    p.bye_week,
    COALESCE(b.points, j.points)  AS points,
    b.floor                       AS floor,
    b.ceiling                     AS ceiling,
    a.adp                         AS adp,
    a.stdev                       AS adp_stdev,
    i.status                      AS injury_status
FROM players p
LEFT JOIN projections_blended b
       ON b.player_key = p.player_key AND b.season = :season AND b.week = :week
-- Source preference is FIXED, not a parameter. `build_board` used to accept a
-- `source` argument, bind it, and never reference it - so build_board(source=
-- "espn") silently returned Sleeper numbers. The blend is the answer whenever
-- it exists; raw Sleeper is the fallback for a player the blender has not
-- reached yet. Anything else would be reading one source in isolation, which
-- is what the blend exists to stop.
LEFT JOIN projections j
       ON j.player_key = p.player_key AND j.season = :season AND j.week = :week
      AND j.source = 'sleeper'
-- ADP source preference is deliberate, not "whichever synced last":
-- FantasyPros ECR is the true expert consensus and ships a real standard
-- deviation, which is what the survival model wants. Sleeper ADP is the
-- fallback, and its stdev is only a proxy derived from scoring variants.
LEFT JOIN (
    SELECT player_key, adp, stdev,
           ROW_NUMBER() OVER (
               PARTITION BY player_key
               ORDER BY CASE source
                            WHEN 'fantasypros' THEN 1
                            WHEN 'sleeper'     THEN 2
                            ELSE 3
                        END,
                        fetched_at DESC
           ) AS rn
    FROM adp
) a ON a.player_key = p.player_key AND a.rn = 1
LEFT JOIN (
    SELECT player_key, status,
           ROW_NUMBER() OVER (PARTITION BY player_key ORDER BY observed_at DESC) AS rn
    FROM injuries
) i ON i.player_key = p.player_key AND i.rn = 1
WHERE p.position = :position
  AND COALESCE(b.points, j.points) IS NOT NULL
ORDER BY points DESC
"""
