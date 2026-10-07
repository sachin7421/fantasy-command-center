"""Team defenses get a ground truth too.

`fcc verify-scoring` on 7 Oct 2026 reported every DEF on the roster as
"not-scored": points_actual existed for players only. nflverse's weekly team
stats plus the game score reproduce Yahoo's four listed DEF totals exactly:

    JAX wk1  5 sacks, 1 INT, 1 FR, 10 allowed  -> 5+2+2+4  = 13
    LAC wk2  2 sacks, 1 INT,       26 allowed  -> 2+2+0    =  4
    DET wk3  5 sacks, 1 FR,        24 allowed  -> 5+2+0    =  7
    DET wk4  3 sacks,              32 allowed  -> 3-1      =  2

Those four lines, as nflverse publishes them, are the fixtures below.
"""
from __future__ import annotations

import src.league_bootstrap as bootstrap
from src import db, scoring
from src.idmap import IdMapper
from src.sources.defense_actuals import defense_line, sync_defense_actuals

RULES = scoring.build_from_yahoo(bootstrap.build_settings())


def _team_row(team: str, week: int, **stats) -> dict:
    base = {
        "team": team, "week": week, "season_type": "REG", "opponent_team": "XXX",
        "def_sacks": 0.0, "def_interceptions": 0, "fumble_recovery_opp": 0,
        "def_tds": 0, "def_safeties": 0, "def_punt_blocks": 0, "def_pat_blocks": 0,
        "def_fg_blocks": 0, "special_teams_tds": 0, "def_2pt_made": 0,
        # Present in the feed and NOT a Yahoo category: counting it broke JAX wk1.
        "pt_returned": 1,
    }
    base.update(stats)
    return base


JAX_WK1 = _team_row("JAX", 1, def_sacks=5.0, def_interceptions=1, fumble_recovery_opp=1)
LAC_WK2 = _team_row("LAC", 2, def_sacks=2.0, def_interceptions=1)
DET_WK3 = _team_row("DET", 3, def_sacks=5.0, fumble_recovery_opp=1)
DET_WK4 = _team_row("DET", 4, def_sacks=3.0)


def _score(row, allowed):
    return round(RULES.score(defense_line(row, points_allowed=allowed), "DT"), 2)


def test_the_four_rostered_defenses_score_what_yahoo_listed():
    assert _score(JAX_WK1, 10) == 13.0
    assert _score(LAC_WK2, 26) == 4.0
    assert _score(DET_WK3, 24) == 7.0
    assert _score(DET_WK4, 32) == 2.0


def test_every_defensive_category_the_league_scores_is_supplied():
    line = defense_line(JAX_WK1, points_allowed=10)
    gaps = [
        c.canonical for c in RULES.categories
        if c.enabled and c.position_type == "DT" and c.modifier and c.canonical
        and not c.is_bucket and c.canonical not in line
    ]
    assert gaps == []
    assert "def_pts_allowed" in line


def test_blocked_kicks_and_touchdowns_count():
    row = _team_row("BUF", 5, def_punt_blocks=1, def_fg_blocks=1, def_tds=1,
                    special_teams_tds=1, def_safeties=1)
    line = defense_line(row, points_allowed=0)
    assert line["def_blk_kick"] == 2.0
    assert line["def_td"] == 1.0 and line["def_ret_td"] == 1.0
    # 2 blocks (4) + TD (6) + return TD (6) + safety (2) + shutout (10)
    assert round(RULES.score(line, "DT"), 2) == 28.0


def test_sync_writes_one_actual_per_team_week_keyed_like_the_players_table(tmp_path):
    conn = db.init_db(tmp_path / "d.db", force_sqlite=True)
    idmap = IdMapper(conn)
    for team, name in (("JAX", "Jacksonville Jaguars"), ("DET", "Detroit Lions")):
        idmap.upsert_player(full_name=name, position="DEF", team=team)
    conn.commit()

    games = [
        {"week": 1, "home_team": "JAX", "home_score": 34, "away_team": "CLE", "away_score": 10},
        {"week": 3, "home_team": "BAL", "home_score": 24, "away_team": "DET", "away_score": 27},
    ]
    stored = sync_defense_actuals(
        conn, RULES, season=2026, team_rows=[JAX_WK1, DET_WK3], games=games,
    )
    assert stored == 2
    rows = {
        (r["player_key"], r["week"]): r["points"]
        for r in conn.fetchall("SELECT player_key, week, points FROM player_week_actuals")
    }
    assert rows == {("DEF|JAX", 1): 13.0, ("DEF|DET", 3): 7.0}
    conn.close()


def test_a_team_with_no_game_score_is_skipped_not_scored_as_a_shutout(tmp_path):
    """Missing opponent score must not read as 0 points allowed (+10)."""
    conn = db.init_db(tmp_path / "d.db", force_sqlite=True)
    IdMapper(conn).upsert_player(full_name="Detroit Lions", position="DEF", team="DET")
    conn.commit()
    stored = sync_defense_actuals(conn, RULES, season=2026, team_rows=[DET_WK4], games=[])
    assert stored == 0
    assert conn.fetchone("SELECT COUNT(*) AS n FROM player_week_actuals")["n"] == 0
    conn.close()
