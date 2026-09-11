"""The roster you type in yourself.

Yahoo API approval is an external gate with no timeline, and while it is
closed six of seven season jobs do nothing. Every model they use already
works; they are starved of one input - fifteen names.

Draft mode has always had this ("manual mode is the default and always
works"). Season mode did not, and the compliance refactor turned that from
degraded into dead. This is the missing half.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper

ROSTER_TEXT = """
# My team - one player per line. Slot is optional.
QB   Josh Allen
RB   Jahmyr Gibbs
RB   Bijan Robinson
WR   Puka Nacua
WR   Ja'Marr Chase
TE   Trey McBride
W/R/T  De'Von Achane
W/R/T  Amon-Ra St. Brown
DEF  Houston
BN   Tyjae Spears
"""


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "manual.db", force_sqlite=True)
    idmap = IdMapper(connection)
    for name, pos, team in [
        ("Josh Allen", "QB", "BUF"),
        ("Jahmyr Gibbs", "RB", "DET"),
        ("Bijan Robinson", "RB", "ATL"),
        ("Puka Nacua", "WR", "LA"),
        ("Ja'Marr Chase", "WR", "CIN"),
        ("Trey McBride", "TE", "ARI"),
        ("De'Von Achane", "RB", "MIA"),
        ("Amon-Ra St. Brown", "WR", "DET"),
        ("Houston", "DEF", "HOU"),
        ("Tyjae Spears", "RB", "TEN"),
    ]:
        idmap.upsert_player(full_name=name, position=pos, team=team)
    connection.commit()
    yield connection
    connection.close()


def test_a_pasted_roster_becomes_a_snapshot(conn):
    from src.manual_roster import load_roster

    snap, unmatched = load_roster(conn, ROSTER_TEXT, league_key="nfl.l.796511",
                                  season=2026, week=2, team_key="4")
    assert unmatched == [], f"could not place: {unmatched}"
    assert len(snap.roster_keys("4")) == 10
    assert snap.is_manual is True


def test_slots_are_kept_so_the_recap_knows_who_started(conn):
    from src.manual_roster import load_roster

    snap, _ = load_roster(conn, ROSTER_TEXT, league_key="nfl.l.796511",
                          season=2026, week=2, team_key="4")
    slots = {s.player_key: s.selected_pos for s in snap.roster_spots_for("4")}
    assert slots["tyjae spears|RB"] == "BN"
    assert slots["josh allen|QB"] == "QB"


def test_a_name_with_no_slot_still_counts(conn):
    """Slots are optional; a bare list of names is the fastest thing to paste."""
    from src.manual_roster import load_roster

    snap, unmatched = load_roster(
        conn, "Josh Allen\nJahmyr Gibbs\n", league_key="x", season=2026,
        week=2, team_key="4",
    )
    assert unmatched == []
    assert len(snap.roster_keys("4")) == 2


def test_an_unrecognised_name_is_reported_not_dropped(conn):
    """Silently dropping one costs a starter and says nothing.

    A roster that is short by one produces a confident lineup that is wrong,
    which is worse than refusing.
    """
    from src.manual_roster import load_roster

    _, unmatched = load_roster(
        conn, "Josh Allen\nNobody At All\n", league_key="x", season=2026,
        week=2, team_key="4",
    )
    assert unmatched == ["Nobody At All"]


def test_comments_and_blank_lines_are_ignored(conn):
    from src.manual_roster import load_roster

    snap, unmatched = load_roster(
        conn, "\n# a comment\n\nJosh Allen\n\n", league_key="x", season=2026,
        week=2, team_key="4",
    )
    assert unmatched == []
    assert len(snap.roster_keys("4")) == 1


def test_punctuation_and_suffixes_do_not_break_a_match(conn):
    """Yahoo writes "Ja'Marr"; people paste "JaMarr". Neither should cost a match."""
    from src.manual_roster import load_roster

    snap, unmatched = load_roster(
        conn, "JaMarr Chase\nAmon Ra St Brown\n", league_key="x", season=2026,
        week=2, team_key="4",
    )
    assert unmatched == [], unmatched
    assert len(snap.roster_keys("4")) == 2


def test_a_manual_snapshot_says_what_it_does_not_have(conn):
    """It carries YOUR roster and nothing else - no wire, no rivals.

    Jobs that need those must say so rather than reporting an empty league as
    a quiet week, which is the failure this whole feature exists to end.
    """
    from src.manual_roster import load_roster

    snap, _ = load_roster(conn, ROSTER_TEXT, league_key="x", season=2026,
                          week=2, team_key="4")
    assert snap.free_agents == []
    assert snap.is_manual is True
    assert "roster" in snap.describe_gaps().lower()


def test_a_defence_matches_however_you_name_it(conn):
    """People write "Houston", "Texans", "HOU" or "Houston Texans" - and the
    player table stores the full name.

    A defence is a whole starting slot in this league, so failing to match one
    costs a starter every week. Team abbreviation and either half of the name
    all resolve.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_roster

    IdMapper(conn).upsert_player(
        full_name="Kansas City Chiefs", position="DEF", team="KC"
    )
    conn.commit()

    for written in ("Kansas City", "Chiefs", "KC", "Kansas City Chiefs",
                    "Kansas City DST"):
        snap, unmatched = load_roster(
            conn, f"DEF {written}", league_key="x", season=2026, week=2,
            team_key="4",
        )
        assert unmatched == [], f"{written!r} did not match a defence"
        # Defences are keyed by TEAM, not by name - a defence has no player
        # name of its own, and two "Houston" rows would otherwise collide.
        assert snap.roster_keys("4") == ["DEF|KC"], written
