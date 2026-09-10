"""Yahoo league state, held in memory for one run.

The API agreement signed 2026-09-10 forbids persisting Yahoo Fantasy data to
disk. Before it, rosters, free agents, team budgets and the transaction log
each had a table, and every season job read them with SQL that joined Yahoo
rows to our own players and projections.

That join is the whole design problem. Yahoo's side may not be stored; our side
is far too large to hold in memory. So the join is inverted: the snapshot
carries player KEYS, and each job selects the non-Yahoo data it needs by those
keys. No Yahoo row is ever written, and no query has to reach across two
sources.

What lives here for the run, and dies with it:

    rosters        who is on which team, and in which slot
    free_agents    who is available
    budgets        remaining FAAB and waiver priority per team
    transactions   the raw log, read once to learn bid behaviour

What may still be written down, and why:

    learned FAAB profiles   a shrunk dollars-per-point coefficient is our own
                            output, and no Yahoo fact is recoverable from it
    league scoring rules    transcribed by hand from the settings page, never
                            fetched from the API - this manager's configuration
                            of his own league, and so it lives in config.yaml
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.storage import Database


@dataclass(frozen=True)
class RosterSpot:
    """One player on one team, at one moment."""

    team_key: str
    team_name: str | None
    player_key: str
    selected_pos: str | None = None


@dataclass(frozen=True)
class TeamBudget:
    """A rival's remaining money.

    The sharpest single input the bid model has: a manager sitting on $2 is not
    a rival however aggressively he normally bids.
    """

    team_key: str
    team_name: str | None
    faab_balance: int | None
    waiver_priority: int | None = None


@dataclass
class LeagueSnapshot:
    """Everything Yahoo told us this run. Never written to disk."""

    league_key: str
    season: int
    week: int
    rosters: list[RosterSpot] = field(default_factory=list)
    free_agents: list[str] = field(default_factory=list)
    budgets: dict[str, TeamBudget] = field(default_factory=dict)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    #: Yahoo players no name match could place. Surfaced, never silently
    #: dropped - see YahooIdIndex.
    unmatched: list[str] = field(default_factory=list)

    def roster_keys(self, team_key: str) -> list[str]:
        """Our player keys for one team, in the order Yahoo listed them."""
        return [r.player_key for r in self.rosters if r.team_key == str(team_key)]

    def roster_spots_for(self, team_key: str) -> list[RosterSpot]:
        """One team's spots, keeping the slot each player currently occupies.

        `roster_keys` is enough for most queries; the lineup optimiser also
        needs to know what is CURRENTLY started, because its whole output is
        the difference between that and the best legal arrangement.
        """
        return [r for r in self.rosters if r.team_key == str(team_key)]

    def all_rostered(self) -> set[str]:
        """Everyone owned by anybody - i.e. everyone NOT on the wire."""
        return {r.player_key for r in self.rosters}

    def budget_of(self, team_key: str) -> int | None:
        entry = self.budgets.get(str(team_key))
        return entry.faab_balance if entry else None

    def team_name(self, team_key: str) -> str | None:
        entry = self.budgets.get(str(team_key))
        if entry and entry.team_name:
            return entry.team_name
        for spot in self.rosters:
            if spot.team_key == str(team_key) and spot.team_name:
                return spot.team_name
        return None


class YahooIdIndex:
    """Maps a Yahoo player onto our player key, without writing anything down.

    Two routes, in order of reliability.

    Sleeper publishes Yahoo cross-reference ids in its own free API, and we
    have stored them since long before this agreement. That is Sleeper's data,
    not data obtained from Yahoo, so it stays - and where it exists the join is
    exact. The Yahoo client itself writes no identifier, ever.

    Where Sleeper has no entry, names are matched instead. That is the weak
    route, so it is built to fail loudly: everything it cannot place goes in
    `unmatched` for the caller to report. A silently dropped player shrinks the
    roster the lineup optimiser sees and produces confident advice about an
    incomplete team - the exact class of quiet degradation this project keeps
    having to fix.
    """

    def __init__(self, conn: Database):
        self.conn = conn
        self.unmatched: list[str] = []
        self._by_name_pos: dict[tuple[str, str], str] = {}
        self._by_name: dict[str, list[str]] = {}
        self._by_yahoo_id: dict[str, str] = {}
        self._load()

    @staticmethod
    def _norm(name: str) -> str:
        """Fold the punctuation that differs between feeds.

        Yahoo writes "Ja'Marr Chase" and other sources write "JaMarr Chase";
        suffixes travel as "Jr." or "Jr". Neither difference should cost a
        match.
        """
        cleaned = "".join(c.lower() for c in str(name) if c.isalnum() or c.isspace())
        parts = [p for p in cleaned.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
        return " ".join(parts)

    def _load(self) -> None:
        for row in self.conn.fetchall(
            "SELECT player_key, full_name, position, yahoo_id FROM players"
        ):
            if row["yahoo_id"]:
                self._by_yahoo_id.setdefault(str(row["yahoo_id"]), row["player_key"])
            name = self._norm(row["full_name"] or "")
            if not name:
                continue
            position = str(row["position"] or "").upper()
            self._by_name_pos.setdefault((name, position), row["player_key"])
            self._by_name.setdefault(name, []).append(row["player_key"])

    def resolve(self, payload: dict[str, Any]) -> str | None:
        """Our player key for a Yahoo player payload, or None.

        Position first, because a name alone is ambiguous across positions -
        and a defence shares its name with its city.
        """
        # The exact route first, where Sleeper gave us the cross-reference.
        raw_id = payload.get("player_id")
        if not raw_id and payload.get("player_key"):
            raw_id = str(payload["player_key"]).split(".")[-1]
        if raw_id:
            found = self._by_yahoo_id.get(str(raw_id))
            if found:
                return found

        name = payload.get("full_name") or _dig(payload, ["name", "full"])
        if not name:
            return None
        normalised = self._norm(name)
        position = str(
            payload.get("primary_position")
            or payload.get("display_position")
            or _dig(payload, ["selected_position", "position"])
            or ""
        ).upper()

        found = self._by_name_pos.get((normalised, position))
        if found:
            return found

        # No position, or a position that disagrees: fall back to the name, but
        # only when it is unambiguous. Guessing between two players with the
        # same name is worse than reporting that we could not tell.
        candidates = self._by_name.get(normalised, [])
        if len(candidates) == 1:
            return candidates[0]

        self.unmatched.append(str(name))
        return None


def _dig(payload: Any, path: list[str]) -> Any:
    for step in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(step)
    return payload


def key_clause(keys) -> tuple[str, list]:
    """An `IN (...)` clause and its parameters, for a list of player keys.

    The one shared piece of the inverted join. Yahoo's side of every season
    query is now a list of keys held in memory, and our side is selected by
    them - so this appears in six modules and should behave identically in all
    of them.

    An empty list returns a clause that matches NOTHING. That is the whole
    reason this is a function rather than an f-string at each site: `IN ()` is
    a syntax error, and the obvious workaround of dropping the clause turns
    "this team has no players" into "select the entire league". That failure is
    silent and confident - it would hand the lineup optimiser all 3,297 players
    and produce a fictional lineup - which is exactly the class of bug this
    project keeps having to dig out.
    """
    keys = [str(k) for k in keys if k]
    if not keys:
        return "NULL", []
    return ",".join("?" for _ in keys), keys
