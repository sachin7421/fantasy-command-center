"""FAAB bidding: what a player is worth, and what it will take to get him.

Two different questions, routinely conflated:

    "What is he worth to me?"      -> a valuation problem
    "What will it take to win?"    -> an auction problem

The first depends only on your roster. The second depends entirely on eleven
other people, and it is the one that decides whether you get the player.

The data problem
----------------
Yahoo reports the **winning** bid and nothing else. Losing bids are never
exposed. So every observation is censored: for each past auction we learn that
the winner was willing to pay X, and that everyone else was willing to pay less
than X - but not how much less.

That is still informative. Winning bids reveal each manager's *aggressiveness*,
and repeated observations across a season separate the manager who pays up from
the one who never bids double figures. What it cannot reveal is the shape of the
losing tail, so the model deliberately stays coarse: a per-manager scale factor
with a wide spread, rather than a precise bid prediction that the data cannot
support.

The model
---------
Manager i bids roughly in proportion to what a player is worth:

    bid_i = beta_i * value,   beta_i ~ LogNormal(mu_i, sigma)

`beta_i` is dollars per point of rest-of-season value, learned from that
manager's winning bids and shrunk toward the league average - early in a season
nobody has enough history to be characterised on their own.

Two hard constraints make the predictions much sharper than the bid history
alone would:

* **Budget.** A manager with $4 left cannot outbid you at $5, whatever his
  history says. Yahoo publishes every team's remaining balance.
* **Need.** A manager already three deep at the position rarely spends on a
  fourth. Rosters are visible too.

Then, for a candidate bid b:

    P(win) = product over rivals of P(their bid < b)

The recommendation is the smaller of what he is worth to you and what it will
take to win. Maximising expected surplus was tried first and is wrong here: in a
competitive auction surplus is near zero at equilibrium, so it recommends bids
that win about two percent of the time. Maximising win probability is the other
failure - it just overpays. The useful answer is a bid capped by value, with an
honest read of whether it will be enough.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Any, Sequence

from src.analytics import shrinkage
from src.storage import Database

#: Dollars per point of ROS value, before any history exists. A typical league
#: spends most of a 100-dollar budget across a season on a handful of players
#: worth a few points a week each.
LEAGUE_PRIOR_BETA = 1.2
#: Spread of the log-normal bid distribution. Deliberately wide: with only
#: winning bids observed, confident point predictions are not supportable.
BID_LOG_SIGMA = 0.75
#: Winning bids needed before a manager is characterised on his own history.
PROFILE_STABILISATION = 6.0
#: Below this balance a manager is effectively out of the bidding.
NUISANCE_BUDGET = 2


@dataclass
class BidRecord:
    """One observed winning bid."""

    week: int
    team_key: str
    player_key: str | None
    player_name: str
    bid: int
    value: float | None = None       # ROS value at the time, if recoverable

    @property
    def beta(self) -> float | None:
        """Dollars paid per point of value."""
        if not self.value or self.value <= 0:
            return None
        return self.bid / self.value


@dataclass
class ManagerProfile:
    team_key: str
    name: str
    observations: int
    beta: float                      # shrunk dollars-per-point
    raw_beta: float | None           # unshrunk, for transparency
    mean_bid: float
    max_bid: int
    budget_left: int | None = None

    @property
    def confidence(self) -> float:
        return shrinkage.weight(self.observations, k=PROFILE_STABILISATION)

    def describe(self) -> str:
        style = (
            "aggressive" if self.beta > LEAGUE_PRIOR_BETA * 1.35
            else "thrifty" if self.beta < LEAGUE_PRIOR_BETA * 0.7
            else "average"
        )
        budget = f", ${self.budget_left} left" if self.budget_left is not None else ""
        return (
            f"{self.name:<20} {style:<11} ${self.beta:.2f}/pt  "
            f"avg ${self.mean_bid:.0f}  max ${self.max_bid}  "
            f"({self.observations} bids{budget})"
        )

    def expected_bid(self, value: float) -> float:
        """What this manager would typically bid for a player of this value."""
        raw = self.beta * max(0.0, value)
        if self.budget_left is not None:
            return min(raw, float(self.budget_left))
        return raw

    def probability_bids_below(self, amount: float, value: float) -> float:
        """P(this manager bids strictly less than `amount`).

        Budget is a hard ceiling, not a tendency: a manager who cannot afford
        the bid loses with certainty, whatever his history suggests.
        """
        if self.budget_left is not None:
            if self.budget_left < amount:
                return 1.0
            if self.budget_left <= NUISANCE_BUDGET:
                return 1.0
        expected = max(self.beta * max(value, 0.01), 0.01)
        if amount <= 0:
            return 0.0
        # Log-normal with median `expected`.
        z = math.log(amount / expected) / BID_LOG_SIGMA
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --- learning from history ---------------------------------------------------

def parse_bids(conn: Database, league_key: str) -> list[BidRecord]:
    """Pull winning FAAB bids out of the stored Yahoo transaction log."""
    rows = conn.fetchall(
        "SELECT txn_id, type, timestamp, payload_json FROM transactions "
        "WHERE league_key=?",
        (league_key,),
    )
    out: list[BidRecord] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue

        bid = payload.get("faab_bid")
        if bid in (None, "", 0):
            continue
        try:
            bid = int(bid)
        except (TypeError, ValueError):
            continue

        team_key, player_key, player_name = _extract_add(payload)
        if not team_key:
            continue
        out.append(
            BidRecord(
                week=int(payload.get("week") or 0),
                team_key=str(team_key),
                player_key=player_key,
                player_name=player_name or "",
                bid=bid,
            )
        )
    return out


def _extract_add(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Find the team that added, and the player added, in a transaction blob.

    Yahoo nests this differently depending on whether the transaction was a
    straight add or an add/drop pair, so both shapes are handled.
    """
    players = payload.get("players")
    if isinstance(players, dict):
        players = list(players.values())
    if not isinstance(players, list):
        players = []

    for entry in players:
        player = entry.get("player", entry) if isinstance(entry, dict) else {}
        data = player.get("transaction_data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        if str(data.get("type")) != "add":
            continue
        name = player.get("full_name") or (player.get("name") or {}).get("full")
        return (
            data.get("destination_team_key") or data.get("destination_team_id"),
            str(player.get("player_id")) if player.get("player_id") else None,
            name,
        )
    return None, None, None


def attach_values(conn: Database, records: Sequence[BidRecord], season: int) -> None:
    """Fill in what each player was worth when he was claimed.

    Uses the season projection stored at the time. Without it a bid cannot be
    turned into dollars-per-point, and the manager profile falls back to raw bid
    sizes only.
    """
    for record in records:
        if not record.player_key:
            continue
        row = conn.fetchone(
            "SELECT points FROM projections_blended "
            "WHERE player_key=? AND season=? AND week=0",
            (record.player_key, season),
        )
        if row and row["points"]:
            # Value is what he is worth ABOVE a freely available player, not his
            # gross projection - nobody bids on gross points.
            record.value = max(0.0, float(row["points"]) * 0.12)


def learn_profiles(
    records: Sequence[BidRecord],
    team_names: dict[str, str] | None = None,
    budgets: dict[str, int] | None = None,
) -> dict[str, ManagerProfile]:
    """Characterise each manager from their winning bids."""
    team_names = team_names or {}
    budgets = budgets or {}

    grouped: dict[str, list[BidRecord]] = {}
    for record in records:
        grouped.setdefault(record.team_key, []).append(record)

    # League prior: the average dollars-per-point across everyone with values.
    all_betas = [r.beta for r in records if r.beta is not None and r.beta > 0]
    league_beta = fmean(all_betas) if all_betas else LEAGUE_PRIOR_BETA

    profiles: dict[str, ManagerProfile] = {}
    for team_key, bids in grouped.items():
        betas = [b.beta for b in bids if b.beta is not None and b.beta > 0]
        raw = fmean(betas) if betas else None
        profiles[team_key] = ManagerProfile(
            team_key=team_key,
            name=team_names.get(team_key, f"Team {team_key}"),
            observations=len(bids),
            # Few bids means the league average is the better guess.
            beta=shrinkage.shrink(raw, league_beta, len(betas), k=PROFILE_STABILISATION),
            raw_beta=round(raw, 3) if raw else None,
            mean_bid=round(fmean(b.bid for b in bids), 1),
            max_bid=max(b.bid for b in bids),
            budget_left=budgets.get(team_key),
        )

    # Managers who have never won a bid still bid; give them the league profile.
    for team_key, name in team_names.items():
        if team_key not in profiles:
            profiles[team_key] = ManagerProfile(
                team_key=team_key, name=name, observations=0, beta=league_beta,
                raw_beta=None, mean_bid=0.0, max_bid=0,
                budget_left=budgets.get(team_key),
            )
    return profiles


# --- bidding -----------------------------------------------------------------

@dataclass
class BidAdvice:
    value: float                  # points of ROS value over replacement
    worth_to_you: int             # the most this is rationally worth, in dollars
    price_to_win: int             # what it will probably take
    recommended: int              # what to actually bid
    win_probability: float
    min_competitive: int
    walk_away: bool = False
    contenders: list[str] = field(default_factory=list)
    curve: list[tuple[int, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.walk_away:
            return (
                f"bid ${self.recommended} at most — the field will likely pay "
                f"about ${self.price_to_win}, well past his value to you"
            )
        return (
            f"bid ${self.recommended} ({self.win_probability:.0%} to win; "
            f"about ${self.price_to_win} is the going rate)"
        )


def win_probability(
    bid: float, rivals: Sequence[ManagerProfile], value: float
) -> float:
    """P(this bid beats every rival)."""
    probability = 1.0
    for rival in rivals:
        probability *= rival.probability_bids_below(bid, value)
    return probability


def market_rate(rivals: Sequence[ManagerProfile]) -> float:
    """Dollars per ROS point this league actually pays."""
    live = [r.beta for r in rivals if r.beta > 0]
    return fmean(live) if live else LEAGUE_PRIOR_BETA


#: ROS points above replacement that would count as a league-winning claim - the
#: kind of add that shows up two or three times a season. Used as the anchor for
#: how much of a budget a player justifies.
ELITE_CLAIM_VALUE = 55.0
#: Most of a remaining budget that any single claim should ever consume.
MAX_BUDGET_FRACTION = 0.6


def worth_to_you(
    value: float, my_budget: int, weeks_left: int = 10
) -> int:
    """The most a player rationally justifies, in dollars.

    Anchored on YOUR budget-allocation problem rather than on what rivals pay.
    Anchoring to the market average is a trap: winning an auction means paying
    more than the average bidder, so a valuation equal to the average recommends
    never bidding at all - technically true, practically useless.

    Concave in value, because the second-best add of the season is worth a lot
    less than the best one, and scaled up late because budget carried past the
    final waiver run is worth nothing.
    """
    if value <= 0 or my_budget <= 0:
        return 0
    urgency = 1.0 + max(0.0, (14 - weeks_left)) / 14 * 0.6
    share = min(1.0, value / ELITE_CLAIM_VALUE) ** 0.7
    return int(max(1, round(my_budget * MAX_BUDGET_FRACTION * share * urgency)))


def recommend(
    value: float,
    my_budget: int,
    rivals: Sequence[ManagerProfile],
    weeks_left: int = 10,
    target_probability: float = 0.7,
) -> BidAdvice:
    """What to bid, and an honest read of whether it will be enough.

    Two numbers the field and your roster decide separately:

        price to win  - what it will probably take, from how this league bids
        worth to you  - the most this player justifies, given your budget

    The recommendation is the smaller of the two: bid what he is worth, never
    more, and be told plainly when that will probably lose. That is more useful
    than either extreme - maximising expected surplus recommends bids that
    almost never win, and maximising win probability recommends overpaying.

    `value` is ROS points above the player he would replace.
    """
    if value <= 0 or my_budget <= 0:
        return BidAdvice(0.0, 0, 0, 0, 0.0, 0, notes=["no value, or no budget left"])

    worth = worth_to_you(value, my_budget, weeks_left)

    curve = [
        (bid, round(win_probability(bid, rivals, value), 4))
        for bid in range(1, my_budget + 1)
    ]

    def bid_for(target: float) -> int:
        for bid, probability in curve:
            if probability >= target:
                return bid
        return my_budget

    price_to_win = bid_for(target_probability)
    min_competitive = bid_for(0.30)

    recommended = max(1, min(price_to_win, worth))
    probability = dict(curve).get(recommended, 0.0)

    # Only counsel walking away when the gap is large. A player who will go for
    # a bit more than he is worth to you is still worth a losing bid: it costs
    # nothing, and rivals sometimes underbid.
    walk_away = price_to_win > worth * 2.0

    contenders = [
        r.name for r in rivals
        if (r.budget_left is None or r.budget_left > max(NUISANCE_BUDGET, recommended))
        and r.expected_bid(value) >= min_competitive * 0.6
    ]

    notes: list[str] = []
    priced_out = [
        r.name for r in rivals
        if r.budget_left is not None and r.budget_left <= NUISANCE_BUDGET
    ]
    if priced_out:
        notes.append(
            f"{len(priced_out)} rival(s) out of money ({', '.join(priced_out[:3])})"
        )
    if walk_away:
        notes.append(
            f"the field will likely pay around ${price_to_win}, well past the "
            f"${worth} he justifies — bid only if he fills a genuine hole"
        )
    elif probability < 0.4:
        notes.append(
            f"${recommended} is what he is worth, but it probably will not win "
            f"(about ${price_to_win} likely needed) — a losing bid still costs nothing"
        )
    elif probability > 0.85 and recommended > 1:
        cheaper = bid_for(0.6)
        if cheaper < recommended:
            notes.append(f"${cheaper} would still win this about 60% of the time")
    if weeks_left <= 4:
        notes.append("late season: unspent budget is worth nothing, so bid up")
    if not any(r.observations for r in rivals):
        notes.append("no bid history yet — this is a value estimate, not a market read")

    return BidAdvice(
        value=round(value, 1),
        worth_to_you=worth,
        price_to_win=price_to_win,
        recommended=recommended,
        win_probability=round(probability, 3),
        min_competitive=min_competitive,
        walk_away=walk_away,
        contenders=contenders[:6],
        curve=curve,
        notes=notes,
    )


def league_report(profiles: dict[str, ManagerProfile]) -> list[str]:
    """How this league bids, so the numbers can be sanity-checked by eye."""
    if not profiles:
        return ["  No FAAB history yet - bids become predictable after a few weeks."]

    ranked = sorted(profiles.values(), key=lambda p: -p.beta)
    lines = ["__HOW THIS LEAGUE BIDS__"]
    lines.extend(f"  {p.describe()}" for p in ranked)

    observed = [p for p in ranked if p.observations]
    if observed:
        betas = [p.beta for p in observed]
        lines.append("")
        lines.append(
            f"  league average ${fmean(betas):.2f}/pt, "
            f"spread {pstdev(betas) if len(betas) > 1 else 0:.2f}"
        )
    return lines
