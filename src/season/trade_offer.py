"""Evaluating a trade somebody offered you.

`trades.py` proposes its own one-for-one ideas. That is not the request a
manager actually gets, which is "Dave offered me Kyren Williams for Puka Nacua
and a bench back - is that good?"

It needs the other team's roster. That cannot be fetched without Yahoo and does
not need to be: you paste it, the same way you paste your own.

The measure is the one the waiver job settled on after getting it wrong once:
what the trade does to your STARTING LINEUP, not to the sum of the players
involved. A deal that upgrades your third receiver while costing you a starting
back is a downgrade, however the raw points read - and raw points are how a bad
trade gets accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.lineup_solver import best_lineup
from src.storage import Database

#: Weeks a season-long projection still covers, for sizing the band. Roughly
#: the remaining regular season at the point trades are actually discussed.
#: Deliberately not exact: the band is an order-of-magnitude statement, and
#: pretending to know it to the week would be the very precision this exists
#: to stop claiming.
TRADE_HORIZON_WEEKS = 10


@dataclass
class _Player:
    player_key: str
    name: str
    position: str
    points: float


@dataclass
class TradeVerdict:
    """What the trade does, and whether to take it."""

    my_before: float = 0.0
    my_after: float = 0.0
    their_before: float | None = None
    their_after: float | None = None
    roster_change: int = 0
    unmatched: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #: Input that cannot describe a real trade - a player arriving who is
    #: already yours, or leaving who is not. Kept apart from `unmatched`
    #: because the app knows exactly who these people are; the TRADE is what
    #: does not make sense.
    impossible: list[str] = field(default_factory=list)

    @property
    def my_gain(self) -> float:
        return round(self.my_after - self.my_before, 2)

    @property
    def their_gain(self) -> float | None:
        if self.their_before is None or self.their_after is None:
            return None
        return round(self.their_after - self.their_before, 2)

    @property
    def accept(self) -> bool | None:
        """True, False, or None when the trade could not be read.

        None rather than False for an unreadable trade: "no" is an answer, and
        giving one about a deal containing a player the app could not identify
        would be inventing it.
        """
        if self.unmatched or self.impossible:
            return None
        return self.my_gain > 0

    def describe(self) -> list[str]:
        if self.impossible:
            return [*self.impossible,
                    "No verdict - that is not a trade that could happen."]
        if self.unmatched:
            return [
                "Could not identify: " + ", ".join(self.unmatched),
                "No verdict - half a trade evaluated is worse than none.",
            ]
        # The band is stated once, in `reasons`. Printing "(+117.0)" here as
        # well would put the false decimal back beside the honest range.
        lines = [
            f"Your starting lineup: {self.my_before:.0f} -> {self.my_after:.0f}"
        ]
        if self.their_gain is not None:
            lines.append(
                f"Theirs: {self.their_before:.0f} -> {self.their_after:.0f} "
                f"({self.their_gain:+.0f})"
            )
        lines.extend(self.reasons)
        return lines


def _load(conn: Database, names, season: int) -> tuple[list[_Player], list[str]]:
    """Resolve names to projected players, reporting what could not be found."""

    found: list[_Player] = []
    missing: list[str] = []
    for name in names:
        row = conn.fetchone(
            "SELECT p.player_key, p.full_name, p.position, b.points "
            "FROM players p LEFT JOIN projections_blended b "
            "  ON b.player_key=p.player_key AND b.season=? AND b.week=0 "
            "WHERE LOWER(p.full_name)=LOWER(?) LIMIT 1",
            (int(season), str(name).strip()),
        )
        if row is None or row["points"] is None:
            missing.append(str(name).strip())
            continue
        found.append(_Player(row["player_key"], row["full_name"],
                             row["position"], float(row["points"])))
    return found, missing


def _lineup_points(players: list[_Player], slots: dict[str, int]) -> float:
    return round(best_lineup(players, slots).total, 2)


def evaluate(
    conn: Database,
    season: int,
    week: int,
    slots: dict[str, int],
    *,
    my_roster: list[str],
    i_give: list[str],
    i_get: list[str],
    their_roster: list[str] | None = None,
) -> TradeVerdict:
    """Score an offered trade against both starting lineups."""
    mine, missing_mine = _load(conn, my_roster, season)
    give, missing_give = _load(conn, i_give, season)
    get, missing_get = _load(conn, i_get, season)

    verdict = TradeVerdict(unmatched=missing_mine + missing_give + missing_get)
    if verdict.unmatched:
        return verdict

    owned = {p.player_key for p in mine}
    for player in get:
        if player.player_key in owned:
            verdict.impossible.append(
                f"{player.name} is already on your roster - you cannot "
                "receive him."
            )
    for player in give:
        if player.player_key not in owned:
            verdict.impossible.append(
                f"{player.name} is not on your roster - you cannot send him."
            )
    if verdict.impossible:
        return verdict

    given = {p.player_key for p in give}
    after = [p for p in mine if p.player_key not in given] + get

    verdict.my_before = _lineup_points(mine, slots)
    verdict.my_after = _lineup_points(after, slots)
    verdict.roster_change = len(get) - len(give)

    if their_roster is not None:
        theirs, missing_theirs = _load(conn, their_roster, season)
        if missing_theirs:
            verdict.unmatched.extend(missing_theirs)
            return verdict
        taken = {p.player_key for p in get}
        their_after = [p for p in theirs if p.player_key not in taken] + give
        verdict.their_before = _lineup_points(theirs, slots)
        verdict.their_after = _lineup_points(their_after, slots)

    verdict.reasons = _reasons(verdict, give, get)
    return verdict


def _reasons(verdict: TradeVerdict, give: list[_Player], get: list[_Player]) -> list[str]:
    reasons: list[str] = []
    from src.analytics.uncertainty import describe, is_meaningful, season_sd

    gain = verdict.my_gain
    # A season total is many weeks of projection error compounded. Stating it
    # as "+117.0" claimed a precision two orders of magnitude finer than the
    # measurement behind it.
    positions: list[str] = [p.position for p in (give + get) if p.position] or [""]
    noise = max(season_sd(p, TRADE_HORIZON_WEEKS, compared=True) for p in positions)
    reasons.append(f"Change to your starting lineup: {describe(gain, noise)}")

    if not is_meaningful(gain, noise):
        reasons.append(
            "That is inside the noise of the projections behind it. Decide on "
            "the players, not on this number."
        )
    elif gain > 0:
        reasons.append("It improves your starting lineup.")
    else:
        reasons.append(
            "It weakens your starting lineup. The players coming in may "
            "outscore the ones leaving, but not in the slots you can start "
            "them in."
        )

    if verdict.roster_change > 0:
        reasons.append(
            f"You end up {verdict.roster_change} over your roster limit - "
            "someone has to be dropped to make room, and what you lose by "
            "dropping him is NOT counted above."
        )
    elif verdict.roster_change < 0:
        reasons.append(
            f"You end up {abs(verdict.roster_change)} under - a roster spot "
            "opens, which is worth something on a waiver day."
        )

    their = verdict.their_gain
    if their is not None and their < 0 and gain > 0:
        reasons.append(
            "It also weakens their lineup, so they may well decline - a trade "
            "the other manager will not take is not an opportunity."
        )
    return reasons
