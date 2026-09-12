"""The roster you type in yourself.

Yahoo API approval is an external gate with no timeline, and while it is
closed six of seven season jobs do nothing. Every model they use already
works; they are starved of one input - fifteen names.

Draft mode has always had this ("manual mode is the default and always
works"). Season mode did not, and the compliance refactor turned that from
degraded into dead. This is the missing half.
"""
from __future__ import annotations

import pathlib

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


YAHOO_PASTE = """
QB	Josh Allen Buf - QB	@ NYJ W 31-24	24.50
RB	Jahmyr Gibbs Det - RB	vs CHI	18.20
RB	Bijan Robinson Atl - RB	@ MIN	21.10
WR	Puka Nacua LAR - WR	vs SF	14.80
WR	Ja'Marr Chase Cin - WR	@ BAL	 9.30
TE	Trey McBride Ari - TE	vs SEA	11.40
W/R/T	De'Von Achane Mia - RB	@ BUF	16.70
W/R/T	Amon-Ra St. Brown Det - WR	vs CHI	13.90
DEF	Houston Texans Hou - DEF	@ JAX	 8.00
BN	Tyjae Spears Ten - RB	vs IND	 4.20
"""


def test_a_raw_paste_from_the_yahoo_roster_page_works(conn):
    """Copying the roster table gives name, team, opponent and points on one line.

    Asking someone to retype fifteen names by hand invites a typo that costs a
    starter, so the parser takes the paste as it comes: it tries progressively
    shorter prefixes of each line until one matches a player, which strips the
    trailing team, matchup and score without needing to know their format.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_roster

    IdMapper(conn).upsert_player(
        full_name="Houston Texans", position="DEF", team="HOU"
    )
    conn.commit()

    snap, unmatched = load_roster(conn, YAHOO_PASTE, league_key="x",
                                  season=2026, week=2, team_key="4")
    assert unmatched == [], f"could not place: {unmatched}"
    assert len(snap.roster_keys("4")) == 10

    slots = {s.player_key: s.selected_pos for s in snap.roster_spots_for("4")}
    assert slots["josh allen|QB"] == "QB"
    assert slots["tyjae spears|RB"] == "BN"


def test_a_prefix_match_never_beats_an_exact_one(conn):
    """Longest match wins, so a real name is not truncated into a shorter one.

    If someone named "Josh" existed, "Josh Allen Buf - QB" must still resolve
    to Josh Allen rather than stopping at the first thing that matched.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_roster

    IdMapper(conn).upsert_player(full_name="Josh", position="WR", team="NYJ")
    conn.commit()

    snap, unmatched = load_roster(conn, "QB Josh Allen Buf - QB", league_key="x",
                                  season=2026, week=2, team_key="4")
    assert unmatched == []
    assert snap.roster_keys("4") == ["josh allen|QB"]


# --- the real thing ----------------------------------------------------------

REAL_PASTE = (
    pathlib.Path(__file__).parent / "fixtures" / "yahoo_roster_paste.txt"
).read_text(encoding="utf-8")

#: What a person reading that page would write down. The fixture is a verbatim
#: copy of the Butt Fumblers roster page, week 1 2026, pasted by the manager.
EXPECTED = [
    ("QB", "Dak Prescott"),
    ("RB", "Kyren Williams"),
    ("RB", "Travis Etienne Jr."),
    ("WR", "Puka Nacua"),
    ("WR", "Tee Higgins"),
    ("TE", "Tyler Warren"),
    ("W/R/T", "Rome Odunze"),
    ("W/R/T", "Jalen Coker"),
    ("BN", "TreVeyon Henderson"),
    ("BN", "Jacory Croskey-Merritt"),
    ("BN", "Matthew Stafford"),
    ("BN", "Mike Washington Jr."),
    ("BN", "Jake Ferguson"),
    ("BN", "Jordan Love"),
    ("DEF", "Jaguars"),
]


def test_the_real_yahoo_paste_parses():
    """The format my invented fixture got wrong, in every particular.

    A real row is not one line. It is the slot, then the name, then the name
    again concatenated with "Video Forecast" / "Player Note" / an injury
    letter, then "TEAM - POS", then the matchup and a column of numbers. The
    made-up single-line fixture I wrote first shares none of that structure,
    which is what standard 7 means by "mocks lie".
    """
    from src.manual_roster import parse_lines

    parsed = parse_lines(REAL_PASTE)
    assert parsed == EXPECTED, f"got {len(parsed)} entries:\n{parsed}"


def test_every_player_on_the_real_roster_resolves(conn):
    """Fifteen names, and a miss costs a starter.

    `Jaguars` has to find `Jacksonville Jaguars`, `Travis Etienne Jr.` has to
    survive its suffix, and `TreVeyon HendersonO` has to shed the injury flag
    glued to the surname.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_roster

    idmap = IdMapper(conn)
    for name, pos, team in [
        ("Dak Prescott", "QB", "DAL"), ("Kyren Williams", "RB", "LAR"),
        ("Travis Etienne Jr.", "RB", "NO"), ("Puka Nacua", "WR", "LAR"),
        ("Tee Higgins", "WR", "CIN"), ("Tyler Warren", "TE", "IND"),
        ("Rome Odunze", "WR", "CHI"), ("Jalen Coker", "WR", "CAR"),
        ("TreVeyon Henderson", "RB", "NE"),
        ("Jacory Croskey-Merritt", "RB", "WAS"),
        ("Matthew Stafford", "QB", "LAR"), ("Mike Washington Jr.", "RB", "LV"),
        ("Jake Ferguson", "TE", "DAL"), ("Jordan Love", "QB", "GB"),
        ("Jacksonville Jaguars", "DEF", "JAX"),
    ]:
        idmap.upsert_player(full_name=name, position=pos, team=team)
    conn.commit()

    snap, unmatched = load_roster(conn, REAL_PASTE, league_key="nfl.l.796511",
                                  season=2026, week=1, team_key="4")
    assert unmatched == [], f"could not place: {unmatched}"
    assert len(snap.roster_keys("4")) == 15

    slots = {s.player_key: s.selected_pos for s in snap.roster_spots_for("4")}
    assert slots["dak prescott|QB"] == "QB"
    assert slots["jordan love|QB"] == "BN"
    assert slots["DEF|JAX"] == "DEF"


def test_the_trailing_column_headers_are_not_players():
    """The paste ends with a block of table headers - Sack, Safe, Int, TD.

    "Defense/Special Teams", "Fan Pts" and "Blk Kick" are not people, and a
    parser that turned them into roster entries would report a 20-man team and
    five names it could not match.
    """
    from src.manual_roster import parse_lines

    names = [name for _, name in parse_lines(REAL_PASTE)]
    for header in ("Fan Pts", "Proj Pts", "Blk Kick", "Fum Rec",
                   "Defense/Special Teams", "Details"):
        assert header not in names


def test_players_whose_game_is_over_are_marked_locked(conn):
    """A finished game is a fact, and a projection for it is a fiction.

    The real paste has four players whose games were Final. The optimiser
    valued Matthew Stafford at his 17.96 PROJECTION and called him the optimal
    QB - he had already played and scored 5.10, while Dak Prescott's game was
    still to come. Acting on that swaps a known 5.1 for a likely 17, and the
    only reason it was not recommended is that the gain fell under the swap
    threshold by eight tenths of a point.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_roster

    idmap = IdMapper(conn)
    for name, pos, team in [
        ("Matthew Stafford", "QB", "LAR"), ("Kyren Williams", "RB", "LAR"),
        ("Puka Nacua", "WR", "LAR"), ("TreVeyon Henderson", "RB", "NE"),
        ("Dak Prescott", "QB", "DAL"),
    ]:
        idmap.upsert_player(full_name=name, position=pos, team=team)
    conn.commit()

    snap, _ = load_roster(conn, REAL_PASTE, league_key="x", season=2026,
                          week=1, team_key="4")

    assert "matthew stafford|QB" in snap.locked
    assert "kyren williams|RB" in snap.locked
    assert "puka nacua|WR" in snap.locked
    assert "treveyon henderson|RB" in snap.locked
    # Dak plays Sunday night; nothing about him is settled.
    assert "dak prescott|QB" not in snap.locked


# --- storing it, so the hosted app can use it --------------------------------

def test_a_saved_roster_stores_only_our_own_facts(conn):
    """Slots and OUR player keys. No Yahoo numbers, ever.

    The boundary is the same one that let league settings stay in the
    repository: a roster the manager typed is his record of his own team. What
    came off the Yahoo page WITH it - the projections, the start percentages,
    the scores - is Yahoo's product and is discarded by the parser before this
    ever sees it.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_roster, save_roster

    IdMapper(conn).upsert_player(full_name="Dak Prescott", position="QB", team="DAL")
    conn.commit()

    snap, _ = load_roster(conn, "QB Dak Prescott", league_key="nfl.l.796511",
                          season=2026, week=1, team_key="4")
    save_roster(conn, snap, team_key="4")

    row = conn.fetchone("SELECT * FROM my_roster")
    stored = set(row.keys())
    for banned in ("points", "proj", "projection", "pct_owned", "pct_start",
                   "fan_pts", "yahoo_id", "yahoo_key"):
        assert banned not in stored, f"my_roster stores {banned}"
    assert row["player_key"] == "dak prescott|QB"
    assert row["slot"] == "QB"


def test_a_saved_roster_round_trips(conn):
    from src.idmap import IdMapper
    from src.manual_roster import load_from_db, load_roster, save_roster

    idmap = IdMapper(conn)
    for name, pos in (("Dak Prescott", "QB"), ("Kyren Williams", "RB")):
        idmap.upsert_player(full_name=name, position=pos, team="X")
    conn.commit()

    snap, _ = load_roster(conn, "QB Dak Prescott\nBN Kyren Williams",
                          league_key="nfl.l.796511", season=2026, week=1,
                          team_key="4")
    save_roster(conn, snap, team_key="4")

    back = load_from_db(conn, league_key="nfl.l.796511", season=2026, week=1,
                        team_key="4")
    assert back is not None
    slots = {s.player_key: s.selected_pos for s in back.roster_spots_for("4")}
    assert slots == {"dak prescott|QB": "QB", "kyren williams|RB": "BN"}
    assert back.is_manual is True


def test_saving_replaces_rather_than_accumulates(conn):
    """A re-paste is a correction, not an addition.

    Appending would leave dropped players on the roster forever, and the
    lineup optimiser would keep offering someone who is no longer on the team.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_from_db, load_roster, save_roster

    idmap = IdMapper(conn)
    for name, pos in (("Dak Prescott", "QB"), ("Jordan Love", "QB")):
        idmap.upsert_player(full_name=name, position=pos, team="X")
    conn.commit()

    for text in ("QB Dak Prescott", "QB Jordan Love"):
        snap, _ = load_roster(conn, text, league_key="L", season=2026, week=1,
                              team_key="4")
        save_roster(conn, snap, team_key="4")

    back = load_from_db(conn, league_key="L", season=2026, week=1, team_key="4")
    assert back.roster_keys("4") == ["jordan love|QB"]


def test_already_played_is_derived_not_stored(conn):
    """Who has played is a fact about the NFL, and it changes during the week.

    Storing Yahoo's "Final" marker would freeze a point-in-time snapshot that
    is wrong an hour later. A recorded actual score in player_week_actuals -
    our own nflverse data - says the same thing and stays current.
    """
    from src import db as dbm
    from src.idmap import IdMapper
    from src.manual_roster import load_from_db, load_roster, save_roster

    idmap = IdMapper(conn)
    for name, pos in (("Played Already", "QB"), ("Plays Sunday", "QB")):
        idmap.upsert_player(full_name=name, position=pos, team="X")
    conn.execute(
        "INSERT INTO player_week_actuals(player_key, season, week, points, "
        "source, recorded_at) VALUES (?,?,?,?,?,?)",
        ("played already|QB", 2026, 1, 5.1, "nflverse", dbm.utcnow()),
    )
    conn.commit()

    snap, _ = load_roster(conn, "QB Played Already\nBN Plays Sunday",
                          league_key="L", season=2026, week=1, team_key="4")
    save_roster(conn, snap, team_key="4")

    back = load_from_db(conn, league_key="L", season=2026, week=1, team_key="4")
    assert back.locked == {"played already|QB"}


def test_a_game_that_was_final_when_pasted_stays_locked(conn):
    """nflverse publishes results days later, so actuals alone are not enough.

    On the real roster, four games were Final when pasted but nflverse had no
    2026 actuals at all - so deriving "already played" only from
    player_week_actuals returned nothing, and the optimiser was free to
    recommend starting a quarterback who had finished on 5.1 again.

    Finality is MONOTONIC: a game that was final when pasted is still final.
    So the paste's own marker is stored and unioned with the actuals, which
    can only ever be conservative - it cannot wrongly unlock anybody.
    """
    from src.idmap import IdMapper
    from src.manual_roster import load_from_db, load_roster, save_roster

    idmap = IdMapper(conn)
    for name, pos in (("Matthew Stafford", "QB"), ("Dak Prescott", "QB")):
        idmap.upsert_player(full_name=name, position=pos, team="X")
    conn.commit()

    paste = (
        "BN\nMatthew Stafford\nMatthew StaffordPlayer Note\nLAR - QB\n"
        "Final L 7-27 vs SF\n"
        "QB\nDak Prescott\nDak PrescottPlayer Note\nDal - QB\n"
        "Sun 8:20 pm @ NYG\n"
    )
    snap, _ = load_roster(conn, paste, league_key="L", season=2026, week=1,
                          team_key="4")
    assert snap.locked == {"matthew stafford|QB"}
    save_roster(conn, snap, team_key="4")

    # No actuals exist for 2026 at all, exactly as in production.
    back = load_from_db(conn, league_key="L", season=2026, week=1, team_key="4")
    assert back.locked == {"matthew stafford|QB"}, (
        "the paste knew this game was over and the stored roster forgot"
    )
