"""The roster you type in yourself, when Yahoo is not available.

Yahoo API approval is an external gate with no published timeline, and while
it is closed six of seven season jobs do nothing at all. Every model they use
already works. They are starved of one input: fifteen names.

Draft mode has always had this - "manual mode is the default and always works"
- and it is why the draft board was usable on 8 September with no API access.
Season mode had no equivalent, and moving Yahoo league state out of the
database turned that gap from degraded into dead.

What a manual snapshot carries, and what it cannot:

    your roster        yes - which is enough for lineup, byes, recap, startsit,
                       injury monitoring of your own players, and the
                       sell-high half of the waiver report
    the free agents    no - who is available is a fact only Yahoo has
    rival rosters      no - so the trade scout cannot run
    FAAB balances      no - supply yours with `--budget`

Jobs that need what is missing must say so. An empty league reported as a
quiet week is the exact failure this whole feature exists to end.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from src.storage import Database
from src.yahoo_snapshot import LeagueSnapshot, RosterSpot

log = logging.getLogger(__name__)

#: Slot labels that are not a starting position. Anything else is treated as a
#: slot name, so a line reading "W/R/T Achane" records the flex.
BENCH_SLOTS = ("BN", "IR", "IR+", "NA")

#: Recognised slot tokens at the start of a line. Everything else is a name.
SLOT_TOKENS = {
    "QB", "RB", "WR", "TE", "K", "DEF", "DST", "W/R/T", "W/R", "R/W/T",
    "FLEX", "BN", "IR", "IR+", "NA",
}

TEMPLATE = """\
# Your roster, one player per line.
#
# The slot in front of each name is OPTIONAL - a bare list of names works.
# Include it and the recap can tell who you actually started.
#
# Lines beginning with # are ignored.

QB
RB
RB
WR
WR
TE
W/R/T
W/R/T
DEF

BN
BN
BN
BN
BN
"""


def _norm(name: str) -> str:
    """Fold the punctuation that differs between where you copied from.

    Yahoo writes "Ja'Marr Chase" and a paste from elsewhere may say "JaMarr
    Chase"; suffixes travel as "Jr." or "Jr". Neither should cost a match, and
    a lost match costs a starter.
    """
    cleaned = "".join(c.lower() for c in str(name) if c.isalnum() or c.isspace())
    parts = [
        p for p in cleaned.split()
        if p not in ("jr", "sr", "ii", "iii", "iv", "v")
    ]
    return " ".join(parts)


#: "LAR - RB", "Dal - QB", "Jax - DEF". The one reliable landmark in a pasted
#: roster: every player has exactly one, and nothing else looks like it.
_TEAM_POS = re.compile(
    r"^([A-Za-z]{2,3})\s*-\s*(QB|RB|WR|TE|K|DEF|DST)$", re.IGNORECASE
)

#: Glued onto the end of the repeated name line: "Kyren WilliamsVideo
#: ForecastNew Player Note", "Rome OdunzeQNew Player Note". The trailing Q/O/D
#: is an injury flag with no separator, which is why the clean name two lines
#: up is preferred over stripping these.
_NAME_NOISE = re.compile(
    r"(Video Forecast|New Player Note|No new player Notes?|Player Note)+$"
)


def parse_lines(text: str) -> list[tuple[str | None, str]]:
    """(slot, name) for each player in a pasted Yahoo roster.

    A row is not one line. The real structure, verbatim:

        RB                                        <- the slot
        Kyren Williams                            <- the name, clean
        Kyren WilliamsVideo ForecastNew Player Note
        LAR - RB                                  <- team and position
        Final L 7-27 vs SF                        <- the matchup
        11 / 14.00 / 12.90 / 96% / 100% / ...     <- a column of numbers

    So the parse anchors on "TEAM - POS", which every player has exactly one of
    and which nothing else in the page resembles. The name is the line two
    above it when that line is a clean prefix of the one between - which is
    what the repetition gives us - and otherwise the line immediately above
    with the noise stripped. The slot is the most recent slot token seen.

    Anchoring on the numbers or the header text would mean tracking a layout
    Yahoo controls. This tracks one pattern that has to exist for the page to
    make sense.

    A hand-typed list still works: a line that is just "QB Josh Allen", or just
    "Josh Allen", is taken as written.
    """
    lines = [raw.replace("\t", " ").split("#", 1)[0].strip()
             for raw in text.splitlines()]

    anchors = [i for i, line in enumerate(lines) if _TEAM_POS.match(line)]
    if anchors:
        return _parse_table(lines, anchors)

    out: list[tuple[str | None, str]] = []
    for line in lines:
        if not line:
            continue
        head, _, rest = line.partition(" ")
        if head.upper() in SLOT_TOKENS and rest.strip():
            out.append((head.upper(), rest.strip()))
        elif head.upper() in SLOT_TOKENS:
            continue          # a slot with no name yet - an unfilled template row
        else:
            out.append((None, line))
    return out


def _clean_name(lines: list[str], anchor: int) -> str | None:
    """The player's name, from the two lines above a "TEAM - POS" line."""
    above = lines[anchor - 1] if anchor >= 1 else ""
    two_above = lines[anchor - 2] if anchor >= 2 else ""

    # The clean name is repeated with junk appended, so it is a prefix of the
    # line below it. That is the reliable signal; stripping is the fallback.
    if two_above and above.startswith(two_above):
        return two_above

    stripped = _NAME_NOISE.sub("", above).strip()
    # A lone injury letter is left glued to the surname once the notes are off
    # ("TreVeyon HendersonO"), and only there - never inside a real name.
    flagged = (
        stripped
        and len(stripped) > 2
        and stripped[-1] in ("Q", "O", "D", "P")
        and stripped[-2].islower()
    )
    if flagged:
        stripped = stripped[:-1]
    return stripped or None


def _parse_table(lines: list[str], anchors: list[int]) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    for anchor in anchors:
        name = _clean_name(lines, anchor)
        if not name:
            continue

        # The most recent slot token above this player, not counting the
        # position half of a "TEAM - POS" line. The first player's slot is
        # often missing because the paste starts mid-row, so it falls back to
        # the position Yahoo lists him at.
        slot = None
        for i in range(anchor - 1, -1, -1):
            candidate = lines[i].strip().upper()
            if _TEAM_POS.match(lines[i]):
                break                      # reached the previous player
            if candidate in SLOT_TOKENS:
                slot = candidate
                break
        if slot is None:
            match = _TEAM_POS.match(lines[anchor])
            slot = match.group(2).upper() if match else None

        out.append((slot, name))
    return out


def load_roster(
    conn: Database,
    text: str,
    *,
    league_key: str,
    season: int,
    week: int,
    team_key: str,
    team_name: str | None = None,
) -> tuple[LeagueSnapshot, list[str]]:
    """Turn pasted names into a snapshot. Returns (snapshot, unmatched names).

    Unmatched names are RETURNED rather than logged and forgotten. A roster
    short by one produces a confident lineup recommendation for a team that is
    not yours, which is worse than refusing to answer.
    """
    # Indexed two ways. A hyphen collapses to nothing on one side and to a
    # space on the other - "Amon-Ra St. Brown" against "Amon Ra St Brown" -
    # so the space-free form is kept as a fallback. A lost match costs a
    # starter, which is expensive for a difference in punctuation.
    index: dict[str, str] = {}
    squashed: dict[str, str] = {}
    #: Defences, separately. They are stored as "Houston Texans" and people
    #: write "Houston", "Texans", "HOU" or "Houston DST" - and a defence is a
    #: whole starting slot in this league, so a missed match costs a starter
    #: every week rather than a bench spot.
    defences: dict[str, str] = {}

    for row in conn.fetchall(
        "SELECT player_key, full_name, position, team FROM players"
    ):
        key = _norm(row["full_name"] or "")
        if not key:
            continue
        index.setdefault(key, row["player_key"])
        squashed.setdefault(key.replace(" ", ""), row["player_key"])

        if str(row["position"] or "").upper() in ("DEF", "DST"):
            words = key.split()
            # "Kansas City Chiefs" answers to the whole name, the city
            # ("kansas city" - a two-word prefix, not just a word), the
            # nickname, and the abbreviation.
            aliases = {
                key,
                " ".join(words[:-1]),      # the city, however many words it is
                words[-1] if words else "",  # the nickname
                _norm(row["team"] or ""),  # the abbreviation
            }
            for alias in aliases:
                if alias:
                    defences.setdefault(alias, row["player_key"])

    snapshot = LeagueSnapshot(
        league_key=league_key, season=int(season), week=int(week)
    )
    snapshot.is_manual = True
    unmatched: list[str] = []

    def resolve(words: list[str], slot: str | None) -> str | None:
        """Longest prefix of `words` that names a player we know.

        A paste from the Yahoo roster page carries the team, the matchup and
        the week's points on the same line as the name. Rather than teach the
        parser that format - which changes - it tries progressively shorter
        prefixes until one matches. LONGEST first, so "Josh Allen Buf - QB"
        resolves to Josh Allen and is not truncated to a shorter real name.
        """
        for end in range(len(words), 0, -1):
            wanted = _norm(" ".join(words[:end]))
            if not wanted:
                continue
            found = index.get(wanted) or squashed.get(wanted.replace(" ", ""))
            if found:
                return found
            bare = " ".join(
                w for w in wanted.split() if w not in ("dst", "dhst", "d")
            )
            if slot in ("DEF", "DST") or bare in defences:
                found = defences.get(bare) or defences.get(wanted)
                if found:
                    return found
        return None

    for slot, name in parse_lines(text):
        player_key = resolve(name.split(), slot)
        if not player_key:
            unmatched.append(name)
            continue
        snapshot.rosters.append(
            RosterSpot(str(team_key), team_name, player_key, slot)
        )
    return snapshot, unmatched


def roster_path(cfg) -> Path:
    """Where the roster file lives. Configurable, with a sensible default."""
    return Path(str(cfg.get("paths.manual_roster", "data/roster.txt")))


def load_from_file(
    conn: Database, path: Path, **kwargs
) -> tuple[LeagueSnapshot, list[str]] | None:
    """The roster file, or None when there is not one yet."""
    if not path.exists():
        return None
    return load_roster(conn, path.read_text(encoding="utf-8"), **kwargs)


def write_template(path: Path) -> Path:
    """Create the file to fill in. Never overwrites an existing roster."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path
