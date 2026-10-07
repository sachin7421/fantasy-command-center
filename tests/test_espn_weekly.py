"""Weekly projections from more than one source.

The data audit of 7 Oct 2026 found every weekly projection in the database
came from Sleeper: ESPN was synced for the season only (week 0), so the
weekly "blend" the lineup runs on was one source. ESPN's payload carries a
projection split for every week of the season under our own scoring; the
sync only had to ask for the week. The fixture is a real payload, trimmed
to three players and the splits this test needs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.league_bootstrap as bootstrap
from src import db, scoring
from src.idmap import IdMapper
from src.sources.espn import EspnSource

FIXTURE = Path(__file__).parent / "fixtures" / "espn_kona_week5_trimmed.json"


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "e.db", force_sqlite=True)
    idmap = IdMapper(conn)
    for name, pos, team in (("Dak Prescott", "QB", "DAL"), ("Puka Nacua", "WR", "LAR"),
                            ("Kyren Williams", "RB", "LAR")):
        idmap.upsert_player(full_name=name, position=pos, team=team)
    conn.commit()
    yield conn
    conn.close()


def _source(conn, monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["players"]
    source = EspnSource(conn)
    monkeypatch.setattr(source, "fetch_players", lambda season, limit=500, force=False: payload)
    return source


def test_a_weekly_sync_stores_that_weeks_projection_under_our_scoring(conn, monkeypatch):
    rules = scoring.build_from_yahoo(bootstrap.build_settings())
    stats = _source(conn, monkeypatch).sync(IdMapper(conn), rules, 2026, week=5)
    assert stats["stored"] == 3
    rows = {
        r["player_key"]: r["points"]
        for r in conn.fetchall("SELECT player_key, points FROM projections WHERE source='espn' AND week=5")
    }
    assert set(rows) == {"dak prescott|QB", "puka nacua|WR", "kyren williams|RB"}
    # Scored by THIS league's rules (0.04/pass yd, 4/pass TD, -1 INT...), not
    # ESPN's own total, so it blends with Sleeper on the same scale.
    assert 10 < rows["dak prescott|QB"] < 35
    assert conn.fetchone("SELECT COUNT(*) AS n FROM projections WHERE source='espn' AND week=0")["n"] == 0


def test_the_season_sync_is_unchanged(conn, monkeypatch):
    rules = scoring.build_from_yahoo(bootstrap.build_settings())
    stats = _source(conn, monkeypatch).sync(IdMapper(conn), rules, 2026)
    assert stats["stored"] == 3
    assert conn.fetchone("SELECT COUNT(*) AS n FROM projections WHERE source='espn' AND week=0")["n"] == 3


def test_fcc_sync_asks_espn_for_the_week_too():
    """The wiring, not the source: the source supported weeks all along."""
    import inspect

    from src import cli

    body = inspect.getsource(cli.cmd_sync)
    assert "espn.sync(ctx.idmap, rules, season, week=week" in body, (
        "cmd_sync never asks ESPN for the current week; weekly projections stay single-source"
    )
