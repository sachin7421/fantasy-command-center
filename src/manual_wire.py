"""The waiver wire, pasted from Yahoo's Players page, for one run.

The waiver model has worked all season and has never had an input. Who is
available is a fact only Yahoo holds, the API scope that would supply it has
not been attached, and the Players page (filter: Available) shows it plainly.

Unlike the manager's own roster, the wire is league-wide Yahoo state, so it is
never stored (obligation 1). It is parsed, resolved to our player keys, handed
to the waiver job, and gone when the run ends. Nothing in this module writes.

A player row on that page, verbatim:

    C.J. Stroud                              <- the name, clean
    C.J. StroudVideo ForecastPlayer Note     <- the name again, with noise
    Hou - QB                                 <- team and position (the anchor)
    Sun 1:00 pm vs Cin                       <- the matchup
    W (Sep 18)                               <- roster status: FA, or W (date)
    1 / 8 / 19.59 / ...                      <- a column of numbers, discarded

A row counts only when BOTH the team line and the status line are present.
That is what keeps out the Trade Hub box in the page footer, which names
players who are not available, and a roster pasted into the wrong box, which
has team lines and no status - and would otherwise recommend claiming players
the manager already owns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.manual_roster import _TEAM_POS, _clean_name, name_resolver
from src.storage import Database
from src.yahoo_snapshot import LeagueSnapshot

#: "FA", or "W (Sep 18)" - on waivers until that date.
_STATUS = re.compile(r"^(FA|W \([A-Za-z]{3} \d{1,2}\))$")


@dataclass(frozen=True)
class WirePlayer:
    """One available player as the page lists him."""

    name: str
    team: str
    position: str
    #: True for "W (date)": a waiver claim. False for "FA": added immediately,
    #: first come first served, with no bid.
    on_waivers: bool


@dataclass
class Wire:
    """A pasted wire resolved to our player keys."""

    player_keys: list[str] = field(default_factory=list)
    #: The subset still on waivers. Everyone else is a free add.
    on_waivers: set[str] = field(default_factory=set)
    #: Pasted name -> our key, for every name that resolved.
    resolved: dict[str, str] = field(default_factory=dict)
    #: Names no player matched. Returned, never dropped: a missing name is a
    #: player the report silently cannot recommend.
    unmatched: list[str] = field(default_factory=list)
    #: Names left out because they are on the manager's roster - a paste taken
    #: before an add.
    already_rostered: list[str] = field(default_factory=list)


def parse_wire(text: str) -> list[WirePlayer]:
    """Every player row in a pasted Players page, in page order, once each.

    Several pages pasted together are fine - 25 a page means several pastes -
    and a player appearing on two of them is kept at his first position.
    """
    lines = [raw.replace("\t", " ").split("#", 1)[0].strip() for raw in text.splitlines()]
    seen: set[str] = set()
    out: list[WirePlayer] = []
    for i, line in enumerate(lines):
        team_pos = _TEAM_POS.match(line)
        if not team_pos:
            continue
        status = lines[i + 2] if i + 2 < len(lines) else ""
        if not _STATUS.match(status):
            continue
        name = _clean_name(lines, i)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(WirePlayer(
            name=name,
            team=team_pos.group(1),
            position=team_pos.group(2).upper(),
            on_waivers=status.startswith("W"),
        ))
    return out


def attach_wire(
    conn: Database, snapshot: LeagueSnapshot, team_key: str, texts: list[str]
) -> Wire:
    """Put a pasted wire (one or more pages) onto a snapshot, for this run.

    Fills `free_agents` and `free_adds` and nothing else. The manager's own
    roster is excluded, so a paste taken before an add cannot recommend a
    player he already owns. Returns the Wire so the caller can say what was
    unmatched or skipped.
    """
    wire = load_wire(conn, "\n".join(texts), exclude=set(snapshot.roster_keys(team_key)))
    snapshot.free_agents = list(wire.player_keys)
    snapshot.free_adds = set(wire.player_keys) - wire.on_waivers
    return wire


def load_wire(
    conn: Database, text: str, exclude: set[str] | frozenset[str] = frozenset()
) -> Wire:
    """Parse a pasted wire and resolve it to our player keys. Writes nothing.

    `exclude` is the manager's own roster: anyone on it is left out and named
    in `already_rostered`, so a stale paste cannot recommend a player he owns.
    """
    resolve = name_resolver(conn)
    wire = Wire()
    for player in parse_wire(text):
        key = resolve(player.name.split(), player.position)
        if key is None:
            wire.unmatched.append(player.name)
            continue
        if key in exclude:
            wire.already_rostered.append(player.name)
            continue
        if key in wire.resolved.values():
            continue
        wire.resolved[player.name] = key
        wire.player_keys.append(key)
        if player.on_waivers:
            wire.on_waivers.add(key)
    return wire
