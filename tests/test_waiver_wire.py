"""The waiver report, run on a wire pasted from Yahoo.

Three things the report got wrong or could not say until it had a real wire:

- A free agent ("FA") is added immediately with no bid. The report priced every
  available player as a FAAB claim, which is wrong advice for most of the page.
- Every claim with a positive gain said "only 0% rostered". `load_free_agents`
  hardcodes ownership to 0.0 because nothing supplies it, so the reason was
  invented for every player.
- On a typed-in roster, "not rostered" meant "not on MY team", so every handcuff
  looked available. With a pasted wire, available means on the wire.
"""
from __future__ import annotations

import pathlib

import pytest

from src import db
from src.yahoo_snapshot import LeagueSnapshot, RosterSpot

LEAGUE, SEASON, WEEK, MINE = "nfl.l.796511", 2026, 3, "3"
SLOTS = {"QB": 1, "RB": 2, "WR": 2}


def _player(conn, key, name, position, points, team="NYJ"):
    conn.execute(
        "INSERT INTO players(player_key, full_name, position, team, updated_at) "
        "VALUES (?,?,?,?,?)",
        (key, name, position, team, db.utcnow()),
    )
    conn.execute(
        "INSERT INTO projections_blended(player_key, season, week, points, computed_at) "
        "VALUES (?,?,?,?,?)",
        (key, SEASON, 0, points, db.utcnow()),
    )


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "wire.db", force_sqlite=True)
    for key, name, pos, pts, team in [
        ("qb1|QB", "My Quarterback", "QB", 300.0, "BUF"),
        ("rb1|RB", "My Lead Back", "RB", 220.0, "NYJ"),
        ("rb2|RB", "My Other Back", "RB", 120.0, "DAL"),
        ("wr1|WR", "My Receiver", "WR", 200.0, "MIA"),
        ("wr2|WR", "My Weak Receiver", "WR", 60.0, "SEA"),
        ("cuff|RB", "Jets Backup", "RB", 90.0, "NYJ"),
        ("free|WR", "Free Agent Receiver", "WR", 190.0, "KC"),
        ("claim|WR", "Waiver Receiver", "WR", 180.0, "LAC"),
    ]:
        _player(connection, key, name, pos, pts, team)
    connection.commit()
    yield connection
    connection.close()


def _manual_snapshot() -> LeagueSnapshot:
    snap = LeagueSnapshot(league_key=LEAGUE, season=SEASON, week=WEEK, is_manual=True)
    for key, pos in [("qb1|QB", "QB"), ("rb1|RB", "RB"), ("rb2|RB", "RB"),
                     ("wr1|WR", "WR"), ("wr2|WR", "WR")]:
        snap.rosters.append(RosterSpot(MINE, "Butt Fumblers", key, pos))
    return snap


def _run(conn, snap):
    from src.season import waivers

    return waivers.run(conn, LEAGUE, MINE, SEASON, WEEK, snapshot=snap,
                       uses_faab=True, budget_left=100, value_margin=1.0,
                       starting_slots=SLOTS)


def test_a_free_agent_is_an_add_now_not_a_bid(conn):
    snap = _manual_snapshot()
    snap.free_agents = ["free|WR", "claim|WR"]
    snap.free_adds = {"free|WR"}
    report = _run(conn, snap)

    by_name = {c.add.name: c for c in report.claims}
    free = by_name["Free Agent Receiver"]
    assert free.bid_rec is None and free.bid_min is None and free.bid_max is None
    assert any("no bid" in r for r in free.reasons)
    assert "bid:" not in free.describe(uses_faab=True)

    claim = by_name["Waiver Receiver"]
    assert claim.bid_rec is not None


def test_the_headline_says_add_now_when_the_top_move_is_a_free_agent(conn):
    from src.season import waivers

    snap = _manual_snapshot()
    snap.free_agents = ["free|WR"]
    snap.free_adds = {"free|WR"}
    note = waivers.to_notification(_run(conn, snap), SEASON)
    assert note is not None
    assert note.title.startswith("Add Free Agent Receiver now")


def test_without_status_every_available_player_is_still_a_claim(conn):
    """A Yahoo snapshot carries no FA/W split. Unchanged behaviour: bid on all."""
    snap = _manual_snapshot()
    snap.is_manual = False
    snap.free_agents = ["free|WR", "claim|WR"]
    report = _run(conn, snap)
    assert all(c.bid_rec is not None for c in report.claims)


def test_no_claim_invents_an_ownership_percentage(conn):
    """Regression: every positive claim said "only 0% rostered"."""
    snap = _manual_snapshot()
    snap.free_agents = ["free|WR", "claim|WR"]
    for claim in _run(conn, snap).claims:
        assert not any("rostered" in r for r in claim.reasons), claim.reasons


def test_on_a_typed_roster_a_handcuff_is_available_only_if_it_is_on_the_wire(conn):
    """Regression: with only your own roster known, every backup looked free."""
    snap = _manual_snapshot()
    snap.free_agents = ["free|WR"]
    assert _run(conn, snap).handcuffs == []

    snap.free_agents = ["free|WR", "cuff|RB"]
    cuffs = _run(conn, snap).handcuffs
    assert [h["handcuff"] for h in cuffs] == ["Jets Backup"]


def test_with_yahoo_rosters_an_unrostered_handcuff_is_still_reported(conn):
    snap = _manual_snapshot()
    snap.is_manual = False
    snap.free_agents = ["free|WR"]
    assert [h["handcuff"] for h in _run(conn, snap).handcuffs] == ["Jets Backup"]


# --- attaching a pasted wire to the snapshot ---------------------------------


WIRE = """
Jets Backup
Jets BackupPlayer Note
NYJ - RB
Sun 1:00 pm vs GB
FA
0

Waiver Receiver
Waiver ReceiverPlayer Note
LAC - WR
Sun 4:05 pm vs LV
W (Jan 1)
0

My Weak Receiver
My Weak ReceiverPlayer Note
Sea - WR
Sun 4:25 pm @ Ari
FA
0

Nobody We Know
Nobody We KnowPlayer Note
Ten - QB
Sun 1:00 pm vs Phi
FA
0
"""


def test_attach_wire_fills_the_snapshot_and_reports_what_it_skipped(conn):
    from src.manual_wire import attach_wire

    snap = _manual_snapshot()
    wire = attach_wire(conn, snap, MINE, [WIRE])
    assert snap.free_agents == ["cuff|RB", "claim|WR"]
    assert snap.free_adds == {"cuff|RB"}
    assert wire.unmatched == ["Nobody We Know"]
    assert wire.already_rostered == ["My Weak Receiver"]


def test_attach_wire_accepts_several_pages(conn):
    from src.manual_wire import attach_wire

    first, second = WIRE.split("Waiver Receiver\n", 1)
    snap = _manual_snapshot()
    attach_wire(conn, snap, MINE, [first, "Waiver Receiver\n" + second])
    assert snap.free_agents == ["cuff|RB", "claim|WR"]


def test_the_real_page_fixture_attaches_without_error(conn):
    from src.manual_wire import attach_wire

    text = pathlib.Path("tests/fixtures/yahoo_wire_paste.txt").read_text(encoding="utf-8")
    snap = _manual_snapshot()
    wire = attach_wire(conn, snap, MINE, [text])
    assert snap.free_agents == []
    assert len(wire.unmatched) == 25


# --- the CLI door: `fcc waivers --wire-file` ---------------------------------


class _Ctx:
    def __init__(self, conn):
        self.conn = conn


def test_no_wire_on_a_typed_roster_says_how_to_get_one(conn, capsys):
    """An empty report here read as "nothing worth claiming". It was nothing
    to look at."""
    from src import cli

    code = cli._attach_pasted_wire(_Ctx(conn), _manual_snapshot(), MINE, [])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "--wire-file" in out
    assert "Players" in out


def test_a_wire_file_is_attached_and_its_misses_are_named(conn, tmp_path, capsys):
    from src import cli

    page = tmp_path / "wire.txt"
    page.write_text(WIRE, encoding="utf-8")
    snap = _manual_snapshot()
    assert cli._attach_pasted_wire(_Ctx(conn), snap, MINE, [str(page)]) is None
    out = capsys.readouterr().out
    assert snap.free_agents == ["cuff|RB", "claim|WR"]
    assert "2 available" in out and "1 free add" in out
    assert "Nobody We Know" in out
    assert "My Weak Receiver" in out


def test_a_file_with_no_wire_rows_fails_loudly(conn, tmp_path, capsys):
    """Most likely the roster page pasted by mistake."""
    from src import cli

    page = tmp_path / "roster.txt"
    page.write_text(
        pathlib.Path("tests/fixtures/yahoo_roster_paste.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    code = cli._attach_pasted_wire(_Ctx(conn), _manual_snapshot(), MINE, [str(page)])
    assert code == cli.EXIT_FAIL
    assert "no available players" in capsys.readouterr().out.lower()


def test_an_unreadable_wire_file_fails_with_its_path(conn, tmp_path, capsys):
    from src import cli

    missing = tmp_path / "nope.txt"
    code = cli._attach_pasted_wire(_Ctx(conn), _manual_snapshot(), MINE, [str(missing)])
    assert code == cli.EXIT_FAIL
    assert "nope.txt" in capsys.readouterr().out


def test_with_yahoo_connected_a_pasted_wire_is_ignored_and_said_so(conn, tmp_path, capsys):
    from src import cli

    page = tmp_path / "wire.txt"
    page.write_text(WIRE, encoding="utf-8")
    snap = _manual_snapshot()
    snap.is_manual = False
    snap.free_agents = ["free|WR"]
    assert cli._attach_pasted_wire(_Ctx(conn), snap, MINE, [str(page)]) is None
    assert snap.free_agents == ["free|WR"]
    assert "ignor" in capsys.readouterr().out


def test_waivers_accepts_wire_file_more_than_once():
    from src import cli

    args = cli.build_parser().parse_args(
        ["waivers", "--wire-file", "a.txt", "--wire-file", "b.txt"]
    )
    assert args.wire_file == ["a.txt", "b.txt"]
