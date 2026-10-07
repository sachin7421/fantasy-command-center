"""The season set-up must include the CURRENT week's game.

Found by the 7 Oct 2026 code review: `playoff_snapshot` fetched scoreboards
from week+1 and `remaining_matchups` kept only later weeks, so the current
week was never simulated and "this week vs X" named NEXT week's opponent -
the lineup's posture and the mailed stakes were priced against the wrong
team. Caught by no test: the payout tests hand-built a schedule that already
included the current week.
"""
from __future__ import annotations

from types import SimpleNamespace

from src import cli
from src.yahoo_snapshot import LeagueSnapshot, TeamStanding


class _Yahoo:
    def __init__(self):
        self.scoreboard_weeks: list[int] = []

    def new_snapshot(self, season, week):
        return LeagueSnapshot(league_key="nfl.l.1", season=season, week=week)

    def fetch_standings(self, force=False):
        return {}

    def collect_standings(self, snapshot, standings):
        for key, name in (("3", "Butt Fumblers"), ("7", "Pipelayers"), ("9", "Dirties")):
            snapshot.standings[key] = TeamStanding(team_key=key, team_name=name, rank=None,
                                                   wins=3, losses=1, ties=0, points_for=400.0)

    def fetch_scoreboard(self, week, force=False):
        self.scoreboard_weeks.append(week)
        return week

    def collect_matchups(self, snapshot, scoreboard, week):
        # Week 5: us vs Dirties. Week 6: us vs Pipelayers.
        opp = {5: "9", 6: "7"}.get(week, "7")
        snapshot.matchups.append((week, "3", opp))


def _ctx(monkeypatch):
    yahoo = _Yahoo()
    ctx = cli.Context.__new__(cli.Context)
    ctx.cfg = SimpleNamespace(get=lambda k, d=None: {"league.my_team_id": 3, "league.season": 2026}.get(k, d))
    ctx._yahoo = yahoo
    monkeypatch.setattr(cli.Context, "season", property(lambda self: 2026))
    monkeypatch.setattr(cli.Context, "yahoo", property(lambda self: yahoo))
    monkeypatch.setattr(cli.Context, "yahoo_ready", lambda self: True)
    monkeypatch.setattr(cli.Context, "settings", lambda self: {"num_playoff_teams": 6, "playoff_start_week": 15,
                                                               "uses_playoff_reseeding": 0})
    monkeypatch.setattr(cli.Context, "team_key", lambda self: "3")
    return ctx, yahoo


def test_the_current_weeks_game_is_the_first_one_simulated(monkeypatch):
    ctx, yahoo = _ctx(monkeypatch)
    setup, reason = cli._season_setup(ctx, week=5)
    assert setup is not None, reason
    assert 5 in yahoo.scoreboard_weeks, "the current week's scoreboard was never fetched"
    weeks = sorted({m.week for m in setup["remaining"]})
    assert weeks[0] == 5 and weeks[-1] == 14
    first = next(m for m in setup["remaining"] if m.week == 5)
    assert {first.home, first.away} == {"3", "9"}, "week 5 is against Dirties, not Pipelayers"


def test_this_weeks_money_names_this_weeks_opponent(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    win_value, high_value, opponent = cli._this_weeks_money(ctx, week=5)
    assert opponent == "9"
    assert high_value == 50.0
    assert win_value >= 0.0


def test_reseed_is_read_from_yahoos_key(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    monkeypatch.setattr(cli.Context, "settings", lambda self: {"num_playoff_teams": 6, "playoff_start_week": 15,
                                                               "uses_playoff_reseeding": 1})
    setup, _ = cli._season_setup(ctx, week=5)
    assert setup is not None and setup["reseed"] is True
