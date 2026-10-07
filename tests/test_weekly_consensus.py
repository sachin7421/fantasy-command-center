"""A third weekly projection source: FantasyPros' expert consensus, via nflverse.

nflverse mirrors FantasyPros' weekly rankings daily (`load_ff_rankings("week")`):
588 players on 7 Oct 2026 with consensus rank, spread, a rank-to-points value,
the opponent and a start/sit grade. Free, and the broadest consensus there
is. The one catch is scale: the points are full-PPR and this league is
half-PPR, so each position's points are calibrated against the week's own
Sleeper/ESPN blend before they join it. A position with too few overlapping
players keeps the raw number and says so.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper
from src.sources.weekly_consensus import WeeklyConsensusSource, calibrate

ROWS = [  # as nflreadpy returns them, trimmed to the columns used
    {"player_name": "Dak Prescott", "pos": "QB", "team": "DAL", "page": "qb", "ecr": 4.8,
     "sd": 1.47, "r2p_pts": 19.2, "player_opponent": "vs. TB", "start_sit_grade": "A",
     "player_game_status": "scheduled", "player_bye_week": 14},
    {"player_name": "Puka Nacua", "pos": "WR", "team": "LAR", "page": "ppr-wr", "ecr": 2.2,
     "sd": 0.87, "r2p_pts": 20.9, "player_opponent": "vs. BUF", "start_sit_grade": "A+",
     "player_game_status": "scheduled", "player_bye_week": 11},
    {"player_name": "New York Jets", "pos": "DST", "team": "NYJ", "page": "dst", "ecr": 14.89,
     "sd": 3.75, "r2p_pts": 6.8, "player_opponent": "vs. CLE", "start_sit_grade": "C",
     "player_game_status": "scheduled", "player_bye_week": 13},
    {"player_name": "Some Linebacker", "pos": "LB", "team": "SF", "page": "lb", "ecr": 1.0,
     "sd": 0.0, "r2p_pts": 15.0, "player_opponent": "at SEA", "start_sit_grade": "A",
     "player_game_status": "scheduled", "player_bye_week": 9},
]


def test_calibration_maps_full_ppr_points_onto_this_leagues_scale():
    """Receivers lose about a quarter of their PPR value in half-PPR: the fit
    should learn that from the overlap and apply it to everyone at the position."""
    ours = {"a": 15.0, "b": 12.0, "c": 9.0, "d": 6.0, "e": 3.0}
    theirs = {"a": 20.0, "b": 16.0, "c": 12.0, "d": 8.0, "e": 4.0, "f": 10.0}
    fit = calibrate(theirs, ours, minimum=5)
    assert fit is not None
    assert fit.apply(10.0) == pytest.approx(7.5, abs=0.01)   # 0.75x, no offset
    assert fit.n == 5


def test_too_small_an_overlap_means_no_calibration():
    assert calibrate({"a": 20.0, "b": 16.0}, {"a": 15.0, "b": 12.0}, minimum=5) is None


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "w.db", force_sqlite=True)
    idmap = IdMapper(conn)
    for name, pos, team in (("Dak Prescott", "QB", "DAL"), ("Puka Nacua", "WR", "LAR"),
                            ("New York Jets", "DEF", "NYJ")):
        idmap.upsert_player(full_name=name, position=pos, team=team)
    conn.commit()
    yield conn
    conn.close()


def test_sync_stores_offense_and_defense_and_skips_idp(conn, monkeypatch):
    source = WeeklyConsensusSource(conn)
    monkeypatch.setattr(source, "fetch_rows", lambda force=False: ROWS)
    stats = source.sync(IdMapper(conn), 2026, 5)
    assert stats["stored"] == 3 and stats["skipped_idp"] == 1
    rows = {
        r["player_key"]: (r["points"], r["stats_json"])
        for r in conn.fetchall("SELECT player_key, points, stats_json FROM projections "
                               "WHERE source='fantasypros' AND season=2026 AND week=5")
    }
    assert set(rows) == {"dak prescott|QB", "puka nacua|WR", "DEF|NYJ"}
    # With no blend to calibrate against, the raw rank-to-points stands, flagged.
    assert rows["dak prescott|QB"][0] == pytest.approx(19.2)
    assert '"calibrated": false' in rows["dak prescott|QB"][1]
    assert '"opponent": "vs. TB"' in rows["dak prescott|QB"][1]
    assert '"grade": "A"' in rows["dak prescott|QB"][1]


def test_sync_calibrates_when_the_weeks_blend_exists(conn, monkeypatch):
    # Eight receivers (the minimum overlap) blended at 75% of their PPR value.
    idmap = IdMapper(conn)
    extra = []
    for i, pts in enumerate((20.0, 18.0, 16.0, 14.0, 12.0, 8.0, 6.0, 4.0)):
        key = idmap.upsert_player(full_name=f"Receiver {i}", position="WR", team="KC")
        conn.execute("INSERT INTO projections_blended(player_key, season, week, points, floor, "
                     "ceiling, stdev, n_sources, detail_json, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (key, 2026, 5, pts * 0.75, 0, 0, 0, 2, "{}", db.utcnow()))
        extra.append(dict(ROWS[1], player_name=f"Receiver {i}", team="KC", r2p_pts=pts))
    conn.commit()
    source = WeeklyConsensusSource(conn)
    monkeypatch.setattr(source, "fetch_rows", lambda force=False: ROWS + extra)
    source.sync(idmap, 2026, 5)
    nacua = conn.fetchone("SELECT points, stats_json FROM projections WHERE player_key=? "
                          "AND source='fantasypros' AND week=5", ("puka nacua|WR",))
    assert nacua["points"] == pytest.approx(20.9 * 0.75, abs=0.05)
    assert '"calibrated": true' in nacua["stats_json"]
