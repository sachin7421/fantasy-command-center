"""One place builds the live league snapshot, for the CLI and the dashboard.

`Context.league_snapshot` held the only copy of "teams, then every roster,
refuse if yours is missing". The hosted dashboard needed the same thing on
7 Oct 2026 when it went from the pasted roster to the live league, so the
body moved to `YahooClient.collect_league` and both call it.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper
from src.yahoo_client import YahooClient


class _Cfg:
    def __init__(self, **values):
        self.values = {"league.league_id": "1", "league.my_team_id": 3, **values}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def require(self, key):
        return self.values[key]

    @property
    def db_path(self):
        return ":memory:"


def _player(name, pos, team="DET", pid="1"):
    return {"name": {"full": name}, "primary_position": pos,
            "editorial_team_abbr": team, "player_id": pid}


class _FakeYahoo(YahooClient):
    """The client with every network method replaced by canned data."""

    def __init__(self, conn, rosters, broken=()):
        super().__init__(_Cfg(), conn)
        self._rosters = rosters
        self._broken = set(broken)
        self._league_key = "470.l.1"

    def fetch_teams(self, force=False):
        return [{"team_id": str(t), "name": f"Team {t}", "faab_balance": 50,
                 "waiver_priority": t} for t in self._rosters]

    def fetch_free_agents(self, count=200, position=None, force=False):
        return [_player("Free Agent", "WR", "CHI", "9")]

    def fetch_transactions(self, force=False):
        return [{"type": "add"}]

    def fetch_roster(self, team_id, week="current", force=False):
        if str(team_id) in self._broken:
            raise RuntimeError("999 rate limited")
        return self._rosters[str(team_id)]


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    idmap = IdMapper(conn)
    idmap.upsert_player(full_name="Jahmyr Gibbs", position="RB", team="DET")
    idmap.upsert_player(full_name="Puka Nacua", position="WR", team="LAR")
    idmap.upsert_player(full_name="Free Agent", position="WR", team="CHI")
    conn.commit()
    yield conn
    conn.close()


ROSTERS = {
    "3": [_player("Jahmyr Gibbs", "RB")],
    "4": [_player("Puka Nacua", "WR", "LAR", "2")],
}


def test_collect_league_gathers_teams_rosters_wire_and_bids(conn):
    snap = _FakeYahoo(conn, ROSTERS).collect_league(2026, 5, my_team_key="3")
    assert set(snap.budgets) == {"3", "4"}
    assert {s.team_key for s in snap.rosters} == {"3", "4"}
    assert len(snap.free_agents) == 1
    assert len(snap.transactions) == 1
    assert snap.unavailable_teams == []


def test_losing_a_rival_roster_is_reported_not_fatal(conn):
    snap = _FakeYahoo(conn, ROSTERS, broken={"4"}).collect_league(2026, 5, my_team_key="3")
    assert snap.unavailable_teams == ["4"]
    assert {s.team_key for s in snap.rosters} == {"3"}


def test_losing_your_own_roster_refuses_to_continue(conn):
    """Every job downstream would otherwise report an empty roster as though
    you had no players, and exit 0."""
    with pytest.raises(RuntimeError, match="your own team"):
        _FakeYahoo(conn, ROSTERS, broken={"3"}).collect_league(2026, 5, my_team_key="3")
