"""The waiver wire you paste yourself, while Yahoo has not attached the scope.

The waiver model has worked all season and has never had an input: who is
available is a fact only Yahoo holds. The Players page (filter: Available)
shows it, 25 at a time, and a paste of that page is enough.

The fixture is a real paste with every Yahoo-generated value replaced; see its
header. The wire is never stored - it is league-wide Yahoo state, unlike the
manager's own roster, so it lives for one run.
"""
from __future__ import annotations

import pathlib

import pytest

from src import db
from src.idmap import IdMapper

WIRE_PASTE = pathlib.Path("tests/fixtures/yahoo_wire_paste.txt").read_text(encoding="utf-8")
ROSTER_PASTE = pathlib.Path("tests/fixtures/yahoo_roster_paste.txt").read_text(encoding="utf-8")

#: Every player row on the page, in page order.
EXPECTED_NAMES = [
    "C.J. Stroud", "Carson Wentz", "Daniel Jones", "Drew Lock", "Aaron Rodgers",
    "Jacoby Brissett", "Geno Smith", "Deshaun Watson", "Kirk Cousins",
    "Cooper Rush", "Cam Ward", "Jayden Reed", "Tua Tagovailoa", "Ryan Flournoy",
    "Malik Washington", "DeMario Douglas", "Adonai Mitchell", "George Holani",
    "Pat Freiermuth", "Jalen Nailor", "Keenan Allen", "Brenton Strange",
    "AJ Barner", "Greg Dulcich", "Tre' Harris",
]
#: "W (date)" rather than "FA": a claim, not a free add.
ON_WAIVERS = {"C.J. Stroud", "Jayden Reed", "Jalen Nailor", "Keenan Allen", "AJ Barner"}


def test_the_real_page_parses_to_every_player_row():
    from src.manual_wire import parse_wire

    assert [p.name for p in parse_wire(WIRE_PASTE)] == EXPECTED_NAMES


def test_position_and_team_come_from_the_team_line():
    from src.manual_wire import parse_wire

    by_name = {p.name: p for p in parse_wire(WIRE_PASTE)}
    assert (by_name["George Holani"].team, by_name["George Holani"].position) == ("Sea", "RB")
    assert (by_name["Pat Freiermuth"].team, by_name["Pat Freiermuth"].position) == ("Pit", "TE")


def test_waivers_and_free_agents_are_told_apart():
    """A free agent is added now for nothing; bidding on one is wrong advice."""
    from src.manual_wire import parse_wire

    parsed = parse_wire(WIRE_PASTE)
    assert {p.name for p in parsed if p.on_waivers} == ON_WAIVERS
    assert sum(1 for p in parsed if not p.on_waivers) == 20


def test_names_in_the_page_footer_are_not_available_players():
    """The Trade Hub box names Chig Okonkwo, who is not on the wire at all.

    Only a row with a team line AND a roster-status line counts.
    """
    from src.manual_wire import parse_wire

    names = [p.name for p in parse_wire(WIRE_PASTE)]
    assert "Chig Okonkwo" not in names
    assert names.count("Jayden Reed") == 1


def test_a_roster_pasted_into_the_wire_box_yields_nobody():
    """Your own roster has team lines and no FA/W status. Reading it as the
    wire would recommend claiming players you already own."""
    from src.manual_wire import parse_wire

    assert parse_wire(ROSTER_PASTE) == []


def test_two_pages_pasted_together_are_not_double_counted():
    """25 a page means several pastes, and pages overlap when sorting shifts."""
    from src.manual_wire import parse_wire

    assert [p.name for p in parse_wire(WIRE_PASTE + "\n" + WIRE_PASTE)] == EXPECTED_NAMES


def test_an_injury_flag_glued_to_the_name_is_not_part_of_it():
    from src.manual_wire import parse_wire

    assert "Tua Tagovailoa" in [p.name for p in parse_wire(WIRE_PASTE)]


# --- resolving to our players ------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "wire.db", force_sqlite=True)
    idmap = IdMapper(connection)
    for name, pos, team in [
        ("C.J. Stroud", "QB", "HOU"), ("Carson Wentz", "QB", "MIN"),
        ("Jayden Reed", "WR", "GB"), ("Tre'Harris", "WR", "LAC"),
        ("George Holani", "RB", "SEA"),
    ]:
        idmap.upsert_player(full_name=name, position=pos, team=team)
    connection.commit()
    yield connection
    connection.close()


def test_the_wire_resolves_to_player_keys_and_reports_what_it_could_not(conn):
    from src.manual_wire import load_wire

    wire = load_wire(conn, WIRE_PASTE)
    assert len(wire.player_keys) == 5
    assert "Daniel Jones" in wire.unmatched
    assert len(wire.unmatched) == 20


def test_punctuation_differences_still_match(conn):
    """Yahoo writes "Tre' Harris"; a source may store "Tre'Harris"."""
    from src.manual_wire import load_wire

    wire = load_wire(conn, WIRE_PASTE)
    assert "Tre' Harris" not in wire.unmatched


def test_waiver_status_follows_the_player_key(conn):
    from src.manual_wire import load_wire

    wire = load_wire(conn, WIRE_PASTE)
    names = {key: name for name, key in wire.resolved.items()}
    assert {names[k] for k in wire.on_waivers} == {"C.J. Stroud", "Jayden Reed"}


def test_players_already_on_your_roster_are_dropped_and_counted(conn):
    """A stale wire paste can include someone you have since added."""
    from src.manual_wire import load_wire

    stroud = load_wire(conn, WIRE_PASTE).resolved["C.J. Stroud"]
    wire = load_wire(conn, WIRE_PASTE, exclude={stroud})
    assert stroud not in wire.player_keys
    assert stroud not in wire.on_waivers
    assert wire.already_rostered == ["C.J. Stroud"]


def test_nothing_is_written(conn):
    """Obligation 1: the wire is Yahoo league state and lives for one run."""
    from src.manual_wire import load_wire

    tables = [r[0] for r in conn.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]

    def counts() -> dict[str, int]:
        return {t: conn.fetchall(f'SELECT COUNT(*) FROM "{t}"')[0][0] for t in tables}

    before = counts()
    load_wire(conn, WIRE_PASTE)
    assert counts() == before
