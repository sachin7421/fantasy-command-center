"""Shared fixtures.

`yahoo_settings` mirrors the shape Yahoo actually returns for league settings,
using real NFL stat ids, so the scoring tests exercise the same parsing path the
live client does.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_addoption(parser):
    """`--update-golden` rewrites the stored snapshots in tests/golden/.

    Deliberately a flag and not an environment variable: blessing a change to
    every number the model produces should be something a person typed on
    purpose, and something that shows up in shell history.
    """
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/*.json from the current behaviour",
    )


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_database():
    """Pin the whole suite to SQLite, whatever the ambient configuration says.

    `Config.load()` overlays .env into os.environ, so merely importing the CLI
    makes DATABASE_URL live. A test that then opened a database got the hosted
    league rather than a temp file - which is how a set of fixture players once
    ended up in the production Supabase tables.

    Two guards, because either alone is escapable. Connections are routed to
    SQLite at the one place every caller funnels through, and the Postgres
    connector is replaced with something that fails loudly, so a future test
    that finds another route says so instead of quietly writing to the league.
    `database_url` itself is left alone - it has its own tests.
    """
    from src import db, storage

    original_connect = db._connect
    original_pg = storage.connect_postgres

    def sqlite_only(db_path=None, url=None, same_thread=True, force_sqlite=False):
        return original_connect(db_path, same_thread=same_thread, force_sqlite=True)

    def refuse(url):
        raise AssertionError(
            "a test tried to open the hosted Postgres database; "
            "pass an explicit db path instead"
        )

    db._connect = sqlite_only
    storage.connect_postgres = refuse
    try:
        yield
    finally:
        db._connect = original_connect
        storage.connect_postgres = original_pg


@pytest.fixture(autouse=True)
def _isolate_environment():
    """Undo any environment a test causes to be set.

    `Config.load()` overlays .env into os.environ with `setdefault`, so merely
    constructing a Context leaks that file's keys into the process for every
    later test. One test writing a fixture .env then made an unrelated test
    believe Yahoo was configured, and it went off to authenticate.
    """
    import os

    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _stat(stat_id, name, display_name, position_type, display_only=0):
    return {
        "stat": {
            "stat_id": stat_id,
            "name": name,
            "display_name": display_name,
            "position_type": position_type,
            "is_only_display_stat": display_only,
        }
    }


def _mod(stat_id, value):
    return {"stat": {"stat_id": stat_id, "value": value}}


# A conventional half-PPR league with distance-scored kickers and bucketed DST.
STAT_CATEGORIES = [
    _stat(4, "Passing Yards", "Pass Yds", "O"),
    _stat(5, "Passing Touchdowns", "Pass TD", "O"),
    _stat(6, "Interceptions", "Int", "O"),
    _stat(9, "Rushing Yards", "Rush Yds", "O"),
    _stat(10, "Rushing Touchdowns", "Rush TD", "O"),
    _stat(11, "Receptions", "Rec", "O"),
    _stat(12, "Reception Yards", "Rec Yds", "O"),
    _stat(13, "Reception Touchdowns", "Rec TD", "O"),
    _stat(15, "Return Touchdowns", "Ret TD", "O"),
    _stat(16, "2-Point Conversions", "2-PT", "O"),
    _stat(18, "Fumbles Lost", "Fum Lost", "O"),
    _stat(19, "Field Goals 0-19 Yards", "FG 0-19", "K"),
    _stat(20, "Field Goals 20-29 Yards", "FG 20-29", "K"),
    _stat(21, "Field Goals 30-39 Yards", "FG 30-39", "K"),
    _stat(22, "Field Goals 40-49 Yards", "FG 40-49", "K"),
    _stat(23, "Field Goals 50+ Yards", "FG 50+", "K"),
    _stat(29, "Point After Attempt Made", "PAT Made", "K"),
    _stat(30, "Point After Attempt Missed", "PAT Miss", "K"),
    _stat(31, "Points Allowed", "Pts Allow", "DT", display_only=1),
    _stat(32, "Sack", "Sack", "DT"),
    _stat(33, "Interception", "Int", "DT"),
    _stat(34, "Fumble Recovery", "Fum Rec", "DT"),
    _stat(35, "Touchdown", "TD", "DT"),
    _stat(36, "Safety", "Safe", "DT"),
    _stat(37, "Block Kick", "Blk Kick", "DT"),
    _stat(50, "Points Allowed 0 points", "Pts Allow 0", "DT"),
    _stat(51, "Points Allowed 1-6 points", "Pts Allow 1-6", "DT"),
    _stat(52, "Points Allowed 7-13 points", "Pts Allow 7-13", "DT"),
    _stat(53, "Points Allowed 14-20 points", "Pts Allow 14-20", "DT"),
    _stat(54, "Points Allowed 21-27 points", "Pts Allow 21-27", "DT"),
    _stat(55, "Points Allowed 28-34 points", "Pts Allow 28-34", "DT"),
    _stat(56, "Points Allowed 35+ points", "Pts Allow 35+", "DT"),
]

STAT_MODIFIERS = [
    _mod(4, 0.04), _mod(5, 4), _mod(6, -2),
    _mod(9, 0.1), _mod(10, 6),
    _mod(11, 0.5), _mod(12, 0.1), _mod(13, 6),
    _mod(15, 6), _mod(16, 2), _mod(18, -2),
    _mod(19, 3), _mod(20, 3), _mod(21, 3), _mod(22, 4), _mod(23, 5),
    _mod(29, 1), _mod(30, -1),
    _mod(32, 1), _mod(33, 2), _mod(34, 2), _mod(35, 6), _mod(36, 2), _mod(37, 2),
    _mod(50, 10), _mod(51, 7), _mod(52, 4), _mod(53, 1),
    _mod(54, 0), _mod(55, -1), _mod(56, -4),
]


#: The same payload as a plain constant, so non-fixture code - the shared league
#: builder the invariant and golden suites use - can reach it without pytest.
YAHOO_SETTINGS_FOR_FIXTURE: dict = {
    "stat_categories": {"stats": STAT_CATEGORIES},
    "stat_modifiers": {"stats": STAT_MODIFIERS},
    "roster_positions": [
        {"roster_position": {"position": "QB", "count": 1}},
        {"roster_position": {"position": "WR", "count": 2}},
        {"roster_position": {"position": "RB", "count": 2}},
        {"roster_position": {"position": "TE", "count": 1}},
        {"roster_position": {"position": "W/R/T", "count": 1}},
        {"roster_position": {"position": "K", "count": 1}},
        {"roster_position": {"position": "DEF", "count": 1}},
        {"roster_position": {"position": "BN", "count": 6}},
        {"roster_position": {"position": "IR", "count": 2}},
    ],
    "num_teams": 12,
    "uses_faab": 1,
    "waiver_type": "FR",
}


@pytest.fixture
def yahoo_settings() -> dict:
    import copy

    return copy.deepcopy(YAHOO_SETTINGS_FOR_FIXTURE)


@pytest.fixture
def ppr_settings(yahoo_settings) -> dict:
    """Full-PPR variant, to prove reception value is read from Yahoo, not baked in."""
    import copy

    settings = copy.deepcopy(yahoo_settings)
    for entry in settings["stat_modifiers"]["stats"]:
        if entry["stat"]["stat_id"] == 11:
            entry["stat"]["value"] = 1.0
    return settings
