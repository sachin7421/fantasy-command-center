"""Trade scout (spec 6.5, advisory only).

Finds rosters whose surpluses complement my deficits, and proposes fair swaps
with the value maths shown. This module NEVER contacts anyone and never proposes
a trade in Yahoo - it prints ideas (spec 6.6).

Fairness is judged on starting-lineup improvement for BOTH sides: a trade that
only helps me is one no one accepts, so it is not a useful suggestion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from src.lineup_solver import best_lineup
from src.notify import Notification
from src.storage import Database


@dataclass
class TradePlayer:
    player_key: str
    name: str
    position: str
    team: str
    points: float


@dataclass
class TradeIdea:
    partner_team: str
    partner_name: str
    i_give: list[TradePlayer]
    i_get: list[TradePlayer]
    my_gain: float
    their_gain: float
    rationale: list[str] = field(default_factory=list)

    @property
    def is_mutual(self) -> bool:
        return self.my_gain > 0 and self.their_gain > 0

    def describe(self) -> str:
        give = ", ".join(f"{p.name} ({p.position})" for p in self.i_give)
        get = ", ".join(f"{p.name} ({p.position})" for p in self.i_get)
        line = (
            f"{self.partner_name}: give {give}  ->  get {get}\n"
            f"    you +{self.my_gain:.1f} / them +{self.their_gain:.1f} projected starting pts"
        )
        if self.rationale:
            line += "\n    " + "; ".join(self.rationale)
        return line


def _load_team(
    conn: Database, league_key: str, team_key: str, season: int, week: int
) -> list[TradePlayer]:
    rows = conn.execute(
        """
        SELECT r.player_key, p.full_name, p.position, p.team,
               COALESCE(b.points, j.points, 0) AS pts
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key=r.player_key AND b.season=? AND b.week=0
        LEFT JOIN projections j
               ON j.player_key=r.player_key AND j.season=? AND j.week=0
              AND j.source='sleeper'
        WHERE r.league_key=? AND r.team_key=? AND r.week=?
        """,
        (season, season, league_key, str(team_key), week),
    ).fetchall()
    return [
        TradePlayer(r["player_key"], r["full_name"], r["position"], r["team"] or "",
                    float(r["pts"] or 0))
        for r in rows
    ]


def _teams_in_league(conn: Database, league_key: str, week: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT team_key, team_name FROM rosters WHERE league_key=? AND week=?",
        (league_key, week),
    ).fetchall()
    return [(r["team_key"], r["team_name"] or f"Team {r['team_key']}") for r in rows]


def positional_profile(
    roster: list[TradePlayer], starting_slots: dict[str, int]
) -> dict[str, float]:
    """Surplus/deficit per position: value above the starters already needed."""
    profile: dict[str, float] = {}
    by_pos: dict[str, list[TradePlayer]] = {}
    for p in roster:
        by_pos.setdefault(p.position, []).append(p)
    for pos, players in by_pos.items():
        players.sort(key=lambda p: p.points, reverse=True)
        needed = starting_slots.get(pos, 0)
        starters = players[:needed]
        depth = players[needed:]
        profile[pos] = round(
            sum(p.points for p in depth[:2]) - sum(p.points for p in starters[-1:]), 1
        )
    return profile


def _rationale(
    give, get,
    my_profile: dict[str, float],
    their_profile: dict[str, float],
    my_gain: float,
    their_gain: float,
) -> list[str]:
    """Why this helps, said only where the rosters actually support it."""
    reasons: list[str] = []
    my_surplus = my_profile.get(give.position, 0.0)
    their_surplus = their_profile.get(get.position, 0.0)

    if my_surplus > 0 and their_surplus > 0:
        reasons.append(
            f"you have {my_surplus:.0f} pts of {give.position} sitting on your "
            f"bench, they have {their_surplus:.0f} at {get.position}"
        )
    elif my_surplus > 0:
        reasons.append(
            f"you have {my_surplus:.0f} pts of {give.position} on the bench; "
            f"{get.position} is where your lineup gains"
        )
    else:
        # No surplus either way: the gain comes from the lineup shape, not from
        # depth, and saying "you are deep at RB" here would be false.
        reasons.append(
            f"{get.position} slots into your lineup better than {give.position} "
            f"does, without either side thinning out"
        )
    reasons.append(
        f"lineup effect: you {my_gain:+.1f}, them {their_gain:+.1f} ROS pts"
    )
    return reasons


def run(
    conn: Database,
    league_key: str,
    my_team_key: str,
    season: int,
    week: int,
    starting_slots: dict[str, int],
    max_ideas: int = 3,
    min_mutual_gain: float = 3.0,
) -> list[TradeIdea]:
    mine = _load_team(conn, league_key, my_team_key, season, week)
    if not mine:
        return []
    my_baseline = best_lineup(mine, starting_slots).total

    ideas: list[TradeIdea] = []
    for team_key, team_name in _teams_in_league(conn, league_key, week):
        if str(team_key) == str(my_team_key):
            continue
        theirs = _load_team(conn, league_key, team_key, season, week)
        if not theirs:
            continue
        their_baseline = best_lineup(theirs, starting_slots).total

        # Surplus per position on both sides, so the sentence attached to an
        # idea describes what is actually true of these two rosters. It used to
        # assert "you are deep at X, they are deep at Y" without ever checking,
        # which is a claim the app had not earned - and `positional_profile`
        # was sitting here unused with exactly this job to do.
        my_profile = positional_profile(mine, starting_slots)
        their_profile = positional_profile(theirs, starting_slots)

        # Only consider players who are not my top asset and not their top asset:
        # nobody trades their best player, and proposing it wastes everyone's time.
        my_candidates = sorted(mine, key=lambda p: p.points, reverse=True)[1:8]
        their_candidates = sorted(theirs, key=lambda p: p.points, reverse=True)[1:8]

        for give, get in product(my_candidates, their_candidates):
            if give.position == get.position:
                continue  # a like-for-like swap rarely helps either side
            new_mine = [p for p in mine if p.player_key != give.player_key] + [get]
            new_theirs = [p for p in theirs if p.player_key != get.player_key] + [give]

            my_gain = best_lineup(new_mine, starting_slots).total - my_baseline
            their_gain = best_lineup(new_theirs, starting_slots).total - their_baseline

            if my_gain >= min_mutual_gain and their_gain >= min_mutual_gain:
                ideas.append(
                    TradeIdea(
                        partner_team=str(team_key),
                        partner_name=team_name,
                        i_give=[give],
                        i_get=[get],
                        my_gain=round(my_gain, 1),
                        their_gain=round(their_gain, 1),
                        rationale=_rationale(
                            give, get, my_profile, their_profile,
                            my_gain, their_gain,
                        ),
                    )
                )

    ideas.sort(key=lambda t: min(t.my_gain, t.their_gain), reverse=True)

    # One idea per partner keeps the output readable.
    seen: set[str] = set()
    unique = []
    for idea in ideas:
        if idea.partner_team in seen:
            continue
        seen.add(idea.partner_team)
        unique.append(idea)
    return unique[:max_ideas]


def to_notification(ideas: list[TradeIdea], season: int, week: int) -> Notification | None:
    if not ideas:
        return None
    lines = [f"  {i + 1}. {idea.describe()}" for i, idea in enumerate(ideas)]
    lines.append("")
    lines.append("Advisory only - nothing has been sent to anyone.")
    return Notification(
        title=(
            f"Trade idea: {ideas[0].i_give[0].name} for "
            f"{ideas[0].i_get[0].name} (+{ideas[0].my_gain:.0f})"
            + (f" and {len(ideas) - 1} more" if len(ideas) > 1 else "")
        ),
        lines=lines,
        job="trades",
        urgency="low",
        season=season,
        week=week,
    )
