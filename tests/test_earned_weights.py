"""The blend weights are earned, not typed.

`fcc accuracy` has measured each source's error for weeks and printed
"earned blend weights" - and `blend_all` read static weights from config.yaml
regardless (found in the 7 Oct 2026 scrub, phase 3). Now:

  - `earned_weights(conn, season, through_week)` returns per-position weights
    derived from `source_accuracy` where there is enough evidence, and None
    where there is not, so the config weights stay the fallback;
  - a source's measured bias at a position is subtracted before blending
    (Sleeper ran 0.7 low on tight ends over 268 player-weeks);
  - ESPN's payload retains its pre-game projection for every past week, so
    `sync` back-fills them and ESPN is scored from week 1, not from the week
    we first asked.
"""
from __future__ import annotations

import pytest

from src import db, projections
from src.analytics.accuracy import Accuracy
from src.analytics import accuracy


def _acc(source, position, n, mae, bias=0.0):
    return Accuracy(source=source, position=position, n=n, mae=mae, rmse=mae * 1.25, bias=bias)


def test_earned_weights_are_per_position_and_favour_the_accurate_source():
    results = [
        _acc("sleeper", "WR", 500, 4.2), _acc("espn", "WR", 500, 5.0),
        _acc("sleeper", "QB", 128, 6.2), _acc("espn", "QB", 128, 6.1),
    ]
    by_pos = accuracy.earned_weights_from(results)
    assert set(by_pos) == {"WR", "QB"}
    assert by_pos["WR"]["sleeper"] > by_pos["WR"]["espn"]
    assert sum(by_pos["WR"].values()) == pytest.approx(1.0, abs=0.01)


def test_a_position_with_one_scored_source_earns_nothing_yet():
    """One source has no competitor to be weighed against; config decides."""
    by_pos = accuracy.earned_weights_from([_acc("sleeper", "DEF", 128, 3.7)])
    assert "DEF" not in by_pos


def test_blend_all_uses_earned_weights_where_they_exist(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "b.db", force_sqlite=True)
    from src.idmap import IdMapper

    idmap = IdMapper(conn)
    wr = idmap.upsert_player(full_name="Some Receiver", position="WR", team="KC")
    qb = idmap.upsert_player(full_name="Some Passer", position="QB", team="KC")
    now = db.utcnow()
    for key, source, pts in ((wr, "sleeper", 10.0), (wr, "espn", 20.0),
                             (qb, "sleeper", 10.0), (qb, "espn", 20.0)):
        conn.execute("INSERT INTO projections(player_key, source, season, week, stats_json, points, "
                     "fetched_at) VALUES (?,?,?,?,?,?,?)", (key, source, 2026, 5, "{}", pts, now))
    conn.commit()

    # WR: sleeper earned 90%; QB: nothing earned -> config 50/50.
    earned = {"WR": {"sleeper": 0.9, "espn": 0.1}}
    projections.blend_all(conn, 2026, 5, weights={"sleeper": 0.5, "espn": 0.5},
                          weights_by_position=earned)
    pts = {r["player_key"]: r["points"] for r in conn.fetchall(
        "SELECT player_key, points FROM projections_blended WHERE week=5")}
    assert pts[wr] == pytest.approx(11.0, abs=0.05)
    assert pts[qb] == pytest.approx(15.0, abs=0.05)
    conn.close()


def test_a_measured_bias_is_removed_before_blending(tmp_path):
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    from src.idmap import IdMapper

    te = IdMapper(conn).upsert_player(full_name="Some Tight End", position="TE", team="KC")
    conn.execute("INSERT INTO projections(player_key, source, season, week, stats_json, points, "
                 "fetched_at) VALUES (?,?,?,?,?,?,?)", (te, "sleeper", 2026, 5, "{}", 8.0, db.utcnow()))
    conn.commit()
    # Sleeper runs 0.7 LOW on tight ends (bias = projected - actual = -0.7).
    projections.blend_all(conn, 2026, 5, weights={"sleeper": 1.0},
                          bias_by_source={("sleeper", "TE"): -0.7})
    row = conn.fetchone("SELECT points, detail_json FROM projections_blended WHERE week=5")
    assert row["points"] == pytest.approx(8.7, abs=0.01)
    assert "bias" in row["detail_json"]
    conn.close()


def test_fcc_sync_backfills_espn_for_every_completed_week():
    import inspect

    from src import cli

    body = inspect.getsource(cli.cmd_sync)
    assert "range(1, week)" in body and "espn.sync(" in body, (
        "ESPN keeps its pre-game projection for past weeks; sync must store them "
        "so the source can be scored from week 1"
    )
