"""What the first live `fcc sync-league` found, 7 Oct 2026.

Two defects that only a real Yahoo response could show (engineering
standard 3: never guess an API):

  - the free-agent request got a 400. Our URL ended in `?format=json` and
    yfpy's `get_response` adds `params={"format": "json"}` itself, so Yahoo
    received `?format=json&format=json` and refused it.
  - 16 roster slots were "unmatched": every team defense. Yahoo names them
    by nickname ("Eagles", team "Phi"); our key is DEF|PHI, by team.
"""
from __future__ import annotations

from src import db
from src.idmap import IdMapper
from src.yahoo_snapshot import YahooIdIndex


def test_the_free_agent_url_leaves_the_format_parameter_to_yfpy():
    from src.yahoo_client import free_agent_page_url

    url = free_agent_page_url("470.l.796511", start=25, page=25, position="WR")
    assert "format=" not in url
    assert url.endswith("/league/470.l.796511/players;status=FA;sort=AR;count=25;start=25;position=WR")


def test_a_defense_resolves_by_its_team_not_its_nickname(tmp_path):
    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    try:
        key = IdMapper(conn).upsert_player(
            full_name="Philadelphia Eagles", position="DEF", team="PHI",
        )
        index = YahooIdIndex(conn)
        # The serialised shape of a Yahoo DEF roster entry, recorded 7 Oct 2026.
        assert index.resolve({
            "name": {"full": "Eagles"}, "display_position": "DEF",
            "primary_position": "DEF", "editorial_team_abbr": "Phi",
            "player_key": "470.p.100021", "player_id": "100021",
        }) == key
        assert index.unmatched == []
    finally:
        conn.close()


def test_a_defense_for_a_team_we_do_not_know_is_still_reported(tmp_path):
    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    try:
        index = YahooIdIndex(conn)
        assert index.resolve({
            "name": {"full": "Eagles"}, "display_position": "DEF",
            "editorial_team_abbr": "Phi",
        }) is None
        assert index.unmatched == ["Eagles"]
    finally:
        conn.close()
