"""Protocol D: the scoring engine must reproduce Yahoo's listed weekly points.

Yahoo's numbers are read live and compared in memory; nothing from Yahoo is
written anywhere, including into a test fixture - a stored copy of Yahoo's
points would be Yahoo data persisted to disk, which the API agreement forbids.
So the comparison logic is tested here with shaped-like-Yahoo fakes, and the
live run itself is `test_live_roster_points_match_yahoo` below, which is
opt-in (FCC_LIVE=1) so the gate never depends on Yahoo being up.
"""
from __future__ import annotations

import os

import pytest

from src import db
from src.idmap import IdMapper
from src.verify_scoring import Comparison, compare_roster_points, summarize


def _yahoo_player(name: str, position: str, total: float, team: str = "Dal") -> dict:
    """The serialised shape of a yfpy Player from
    get_team_roster_player_stats_by_week - the fields compare_roster_points
    reads, recorded from a real response on 7 Oct 2026 (values changed)."""
    return {
        "name": {"full": name},
        "display_position": position,
        "primary_position": position,
        "editorial_team_abbr": team,
        "player_points": {"coverage_type": "week", "total": total, "week": 3},
    }


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "v.db", force_sqlite=True)
    idmap = IdMapper(conn)
    for name, pos, points in (("Dak Prescott", "QB", 18.94), ("Jordan Love", "QB", 18.48)):
        key = idmap.upsert_player(full_name=name, position=pos, team="AAA")
        db.record_actuals_many(conn, [(key, 2026, 3, points, None, "nflverse", db.utcnow())])
    conn.commit()
    yield conn
    conn.close()


class _Yahoo:
    def __init__(self, players):
        self.players = players

    def fetch_roster_points(self, team_id, week):
        return self.players


def test_matching_points_are_exact(conn):
    yahoo = _Yahoo([_yahoo_player("Dak Prescott", "QB", 18.94)])
    rows = compare_roster_points(conn, yahoo, team_id=3, season=2026, weeks=[3])
    assert rows == [Comparison(week=3, name="Dak Prescott", position="QB",
                               yahoo=18.94, ours=18.94, verdict="exact")]


def test_a_difference_is_named_with_both_numbers(conn):
    yahoo = _Yahoo([_yahoo_player("Jordan Love", "QB", 19.48)])
    [row] = compare_roster_points(conn, yahoo, team_id=3, season=2026, weeks=[3])
    assert row.verdict == "differs"
    assert (row.yahoo, row.ours) == (19.48, 18.48)


def test_an_inactive_player_with_no_row_is_not_a_difference(conn):
    """Yahoo lists 0.00 for a player who did not play; nflverse has no row.
    That is agreement, not a gap."""
    yahoo = _Yahoo([_yahoo_player("Puka Nacua", "WR", 0.0)])
    [row] = compare_roster_points(conn, yahoo, team_id=3, season=2026, weeks=[3])
    assert row.verdict == "inactive"


def test_a_scoring_player_with_no_row_is_a_gap(conn):
    yahoo = _Yahoo([_yahoo_player("Rome Odunze", "WR", 5.9)])
    [row] = compare_roster_points(conn, yahoo, team_id=3, season=2026, weeks=[3])
    assert row.verdict == "no-row"


def test_defenses_are_compared_by_team(conn):
    """Yahoo names a defense "Detroit" with team "Det"; the engine keys it
    DEF|DET. The two must meet, or every DEF reads as a gap."""
    key = IdMapper(conn).upsert_player(full_name="Detroit Lions", position="DEF", team="DET")
    db.record_actuals_many(conn, [(key, 2026, 3, 7.0, None, "nflverse", db.utcnow())])
    conn.commit()
    yahoo = _Yahoo([_yahoo_player("Detroit", "DEF", 7.0, team="Det")])
    [row] = compare_roster_points(conn, yahoo, team_id=3, season=2026, weeks=[3])
    assert row.verdict == "exact"


def test_summary_counts_and_passes_only_with_enough_exact_and_no_differences():
    rows = [Comparison(3, "A", "QB", 1.0, 1.0, "exact")] * 10
    ok, text = summarize(rows, minimum_exact=10)
    assert ok and "10 exact" in text
    ok, text = summarize(rows + [Comparison(3, "B", "RB", 2.0, 1.0, "differs")], 10)
    assert not ok and "1 differ" in text
    ok, _ = summarize(rows[:9], minimum_exact=10)
    assert not ok, "nine matches prove less than the spec asks for"


@pytest.mark.skipif(
    os.environ.get("FCC_LIVE") != "1",
    reason="live Yahoo comparison; run with FCC_LIVE=1 on a machine with a token",
)
def test_live_roster_points_match_yahoo():
    """Spec section 10, criterion 1, run against the real league.

    Runs the real command in a subprocess because this suite pins every
    in-process connection to SQLite (conftest) - the actuals live in the
    hosted database, and that guard must stay. First passed 7 Oct 2026: 49 of
    57 roster player-weeks over weeks 1-4 exact, zero differences, eight
    inactive. Nothing is stored.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "fcc.py", "verify-scoring"],
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "PASS" in result.stdout, result.stdout[-2000:]


# --- a weekly habit: Monday's run tells you only when something is wrong ------

class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, n, force=False):
        self.sent.append(n)
        return {"sent": True}


def _ctx_for_notify(conn, rows_yahoo):
    class _Yahoo:
        def fetch_roster_points(self, team_id, week):
            return rows_yahoo

    class _Cfg:
        def get(self, key, default=None):
            return {"league.my_team_id": 3, "league.season": 2026}.get(key, default)

    notifier = _Notifier()

    class _Ctx:
        cfg = _Cfg()
        season = 2026
        yahoo = _Yahoo()

        def __init__(self):
            self.conn = conn

        def team_key(self):
            return "3"

        def current_week(self):
            return 4

        def notifier(self):
            return notifier

    return _Ctx(), notifier


def test_a_passing_check_sends_nothing(conn):
    from types import SimpleNamespace

    from src import cli

    ctx, notifier = _ctx_for_notify(conn, [_yahoo_player("Dak Prescott", "QB", 18.94)] * 10)
    code = cli.cmd_verify_scoring(ctx, SimpleNamespace(week=3, notify=True))
    assert code == cli.EXIT_OK
    assert notifier.sent == []


def test_a_difference_is_mailed_when_asked(conn):
    from types import SimpleNamespace

    from src import cli

    ctx, notifier = _ctx_for_notify(conn, [_yahoo_player("Jordan Love", "QB", 19.48)])
    code = cli.cmd_verify_scoring(ctx, SimpleNamespace(week=3, notify=True))
    assert code == cli.EXIT_FAIL
    [note] = notifier.sent
    assert "Jordan Love" in note.text() and "19.48" in note.text()
