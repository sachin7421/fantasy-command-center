"""Phase 1 of the 7 Oct 2026 scrub: what the first look at the live dashboard found.

  1. The "This week" tab defaulted to week 1: it read `league.current_week`
     from config.yaml, which does not exist, so the page showed week-1
     projections against the current roster and looked "all wrong".
  2. Our injury tags disagreed with Yahoo's for the user's own players
     (Sleeper said Nacua, Warren, Henderson and Croskey-Merritt were
     Questionable; Yahoo said Higgins and Coker). Yahoo's tag is what the
     manager sees and what decides lock and eligibility, so for players on a
     live roster it wins. It is read with the roster and never stored.
  3. The league's payouts were nowhere in the code, so nothing could be
     payout-aware. From the commissioner's notes, with the user confirming
     14 regular-season weeks against the note's "x13".
"""
from __future__ import annotations

import src.league_bootstrap as bootstrap
from src import db
from src.idmap import IdMapper
from src.season import lineup
from src.yahoo_snapshot import RosterSpot


# --- 1. the dashboard's week ---------------------------------------------------

def test_the_live_week_comes_from_the_nfl_state_not_a_config_key(monkeypatch):
    from src import schedule

    class _State:
        def __init__(self, conn):
            pass

        def state(self, force=False):
            return {"week": 5, "season_type": "regular"}

    monkeypatch.setattr("src.sources.sleeper.SleeperSource", _State)
    assert schedule.current_week(conn=None) == 5


def test_preseason_or_unknown_state_is_week_one(monkeypatch):
    from src import schedule

    class _State:
        def __init__(self, conn):
            pass

        def state(self, force=False):
            return {"week": 3, "season_type": "pre"}

    monkeypatch.setattr("src.sources.sleeper.SleeperSource", _State)
    assert schedule.current_week(conn=None) == 1


# --- 2. Yahoo's injury tag wins for a live roster ------------------------------

def test_yahoo_status_codes_map_onto_the_lineup_vocabulary():
    assert lineup.yahoo_status("Q") == "Questionable"
    assert lineup.yahoo_status("D") == "Doubtful"
    assert lineup.yahoo_status("O") == "Out"
    assert lineup.yahoo_status("IR") == "IR"
    assert lineup.yahoo_status("IR-R") == "IR"
    assert lineup.yahoo_status("PUP-R") == "PUP"
    assert lineup.yahoo_status("SUSP") == "Suspended"
    assert lineup.yahoo_status("NA") == "NA"
    assert lineup.yahoo_status("") is None
    assert lineup.yahoo_status(None) is None


def test_a_live_roster_tag_overrides_a_stale_feed(tmp_path):
    conn = db.init_db(tmp_path / "l.db", force_sqlite=True)
    idmap = IdMapper(conn)
    nacua = idmap.upsert_player(full_name="Puka Nacua", position="WR", team="LAR")
    higgins = idmap.upsert_player(full_name="Tee Higgins", position="WR", team="CIN")
    # The feed says Nacua is Questionable and Higgins is fine.
    for key, status in ((nacua, "Questionable"), (higgins, None)):
        conn.execute(
            "INSERT INTO injuries(player_key, status, source, observed_at) "
            "VALUES (?,?,?,?)", (key, status, "sleeper", db.utcnow()),
        )
    conn.commit()
    # Yahoo, read with the roster, says the opposite.
    spots = [
        RosterSpot("3", "Butt Fumblers", nacua, "WR", status=""),
        RosterSpot("3", "Butt Fumblers", higgins, "WR", status="Q"),
    ]
    by_name = {p.name: p for p in lineup.load_roster(conn, 2026, 5, spots)}
    assert by_name["Puka Nacua"].injury_status is None
    assert by_name["Tee Higgins"].injury_status == "Questionable"
    conn.close()


def test_a_pasted_roster_without_tags_keeps_the_feed(tmp_path):
    """The typed-in roster carries no status; the feed is all there is."""
    conn = db.init_db(tmp_path / "l.db", force_sqlite=True)
    key = IdMapper(conn).upsert_player(full_name="Puka Nacua", position="WR", team="LAR")
    conn.execute(
        "INSERT INTO injuries(player_key, status, source, observed_at) "
        "VALUES (?,?,?,?)", (key, "Questionable", "sleeper", db.utcnow()),
    )
    conn.commit()
    [player] = lineup.load_roster(conn, 2026, 5, [RosterSpot("3", None, key, "WR")])
    assert player.injury_status == "Questionable"
    conn.close()


# --- 3. the money ---------------------------------------------------------------

def test_the_payouts_add_up_to_the_pot():
    p = bootstrap.PAYOUTS
    assert p["dues"] == 200 and p["teams"] == 12
    pot = p["dues"] * p["teams"]
    assert pot == 2400
    paid = (
        p["weekly_high_score"] * p["weekly_high_score_weeks"]
        + p["most_points_regular_season"]
        + p["consolation_winner"]
        + sum(p["finish"].values())
    )
    assert paid == pot, f"payouts {paid} do not equal the pot {pot}"


def test_the_regular_season_is_fourteen_weeks_as_yahoo_says():
    """Yahoo: playoff_start_week 15, end_week 17 (read live 7 Oct 2026). The
    commissioner's note pays the high score for 13 weeks, and only 13 makes
    the pot balance - so one of the 14 weeks is unpaid, which week unknown."""
    assert bootstrap.PAYOUTS["regular_season_weeks"] == 14
    assert bootstrap.PAYOUTS["weekly_high_score_weeks"] == 13
    assert bootstrap.PLAYOFF_WEEKS == (15, 16, 17)
    assert bootstrap.TRADE_DEADLINE == "2026-11-28"


# --- 2b. Yahoo's tag for every player it exposes, not just your roster ---------

def test_the_snapshot_carries_yahoo_tags_for_rostered_and_free_players(tmp_path):
    """125 of 390 players disagreed between the Sleeper feed and Yahoo on
    7 Oct 2026 - nearly all stale 'Questionable' tags Sleeper carried
    forward. Yahoo's tag arrives with every roster and wire payload."""
    from tests.test_collect_league import _Cfg, _player
    from src.yahoo_client import YahooClient

    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    idmap = IdMapper(conn)
    idmap.upsert_player(full_name="Jahmyr Gibbs", position="RB", team="DET")
    idmap.upsert_player(full_name="Free Agent", position="WR", team="CHI")
    conn.commit()
    client = YahooClient(_Cfg(), conn)
    snap = client.new_snapshot(2026, 5)
    client.collect_roster(snap, "3", [dict(_player("Jahmyr Gibbs", "RB"), status="Q")])
    client.collect_free_agents(snap, [dict(_player("Free Agent", "WR", "CHI", "9"), status="")])
    assert snap.statuses == {"jahmyr gibbs|RB": "Q", "free agent|WR": ""}
    conn.close()


def test_waiver_candidates_take_yahoos_tag_over_the_feed(tmp_path):
    from src.season import waivers
    from src.yahoo_snapshot import LeagueSnapshot

    conn = db.init_db(tmp_path / "w.db", force_sqlite=True)
    idmap = IdMapper(conn)
    key = idmap.upsert_player(full_name="Stale Receiver", position="WR", team="KC")
    conn.execute(
        "INSERT INTO injuries(player_key, status, source, observed_at) VALUES (?,?,?,?)",
        (key, "Out", "sleeper", db.utcnow()),
    )
    conn.commit()
    snap = LeagueSnapshot(league_key="nfl.l.1", season=2026, week=5)
    snap.free_agents = [key]
    snap.statuses = {key: ""}          # Yahoo: no tag today
    [cand] = waivers.load_free_agents(conn, 2026, 5, snap.free_agents, statuses=snap.statuses)
    assert cand.injury_status is None and not cand.is_stash
    conn.close()
