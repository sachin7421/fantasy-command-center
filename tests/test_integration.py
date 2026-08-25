"""Tests that exercise call sites, not just the models behind them.

Every unit test here passed while `fcc faab` crashed on its first line of
output and the waiver job raised on every FAAB claim: the model had been
reshaped and its two callers still read fields that no longer existed. Nothing
in the suite ever ran a command end to end, so nothing noticed.

So these tests deliberately go in through the front door - `cli.main(argv)` and
`waivers.run(...)` - and assert on what a person would actually see.
"""
from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path

import pytest

from src import cli, db

LEAGUE = "nfl.l.796511"
SEASON = 2026
WEEK = 5
MY_TEAM = "3"


# --- the whole package must actually import ---------------------------------

def test_every_module_imports():
    """A syntax check is not an import check.

    `src/ui.py` once parsed perfectly and raised NameError on import, because a
    CSS block full of literal braces had been turned into an f-string. Anything
    that only ran `ast.parse` called it healthy; the dashboard was dead.
    """
    import src

    failures = []
    for module in pkgutil.walk_packages(src.__path__, prefix="src."):
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # pragma: no cover - the failure IS the report
            failures.append(f"{module.name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules that do not import:\n  " + "\n  ".join(failures)


# --- a league with enough state for the FAAB path ---------------------------

def _player(conn, key, name, position, points, yahoo_id=None):
    conn.execute(
        "INSERT INTO players(player_key, yahoo_id, full_name, position, team, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (key, yahoo_id, name, position, "NYJ", db.utcnow()),
    )
    conn.execute(
        "INSERT INTO projections_blended(player_key, season, week, points, computed_at) "
        "VALUES (?,?,?,?,?)",
        (key, SEASON, 0, points, db.utcnow()),
    )


def _bid(conn, txn_id, team_id, yahoo_id, name, amount, week):
    """A Yahoo transaction blob in the shape the live feed uses.

    Note the nesting and the full team key: both were wrong in the first cut of
    the parser, and both are why this fixture is verbose rather than a flat dict.
    """
    payload = {
        "faab_bid": amount,
        "week": week,
        "players": {
            "0": {
                "player": {
                    "player_id": yahoo_id,
                    "name": {"full": name},
                    "transaction_data": {
                        "type": "add",
                        "destination_team_key": f"449.l.12345.t.{team_id}",
                    },
                }
            }
        },
    }
    conn.execute(
        "INSERT INTO transactions(league_key, txn_id, type, timestamp, payload_json) "
        "VALUES (?,?,?,?,?)",
        (LEAGUE, txn_id, "add", "1700000000", json.dumps(payload)),
    )


@pytest.fixture
def league_db(tmp_path):
    path = tmp_path / "integration.db"
    conn = db.init_db(path)

    roster = [
        ("jets qb|QB", "Aaron Rodgers", "QB", 250.0),
        ("jets rb|RB", "Breece Hall", "RB", 210.0),
        ("jets wr|WR", "Garrett Wilson", "WR", 195.0),
        ("bench guy|RB", "Israel Abanikanda", "RB", 60.0),
    ]
    for key, name, pos, pts in roster:
        _player(conn, key, name, pos, pts)
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
            "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (LEAGUE, MY_TEAM, "Butt Fumblers", key, pos, WEEK, db.utcnow()),
        )

    # Rivals, so the model has more than one manager to reason about.
    for team_id, team_name, key, name, pos, pts in [
        ("1", "Team One", "rival a|WR", "Rival A", "WR", 150.0),
        ("2", "Team Two", "rival b|RB", "Rival B", "RB", 140.0),
    ]:
        _player(conn, key, name, pos, pts)
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
            "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (LEAGUE, team_id, team_name, key, pos, WEEK, db.utcnow()),
        )

    # The target: a free agent worth clearly more than the worst roster spot.
    _player(conn, "waiver add|RB", "Waiver Add", "RB", 150.0, yahoo_id="31896")
    conn.execute(
        "INSERT INTO free_agents(league_key, player_key, pct_owned, week, fetched_at) "
        "VALUES (?,?,?,?,?)",
        (LEAGUE, "waiver add|RB", 22.0, WEEK, db.utcnow()),
    )

    # Bid history, so profiles are learned rather than defaulted.
    _bid(conn, "t1", "1", "31896", "Someone", 34, 2)
    _bid(conn, "t2", "1", "31896", "Someone", 41, 3)
    _bid(conn, "t3", "2", "31896", "Someone", 6, 2)
    _bid(conn, "t4", "2", "31896", "Someone", 9, 4)

    for team_id, balance in (("1", 62), ("2", 11), (MY_TEAM, 73)):
        conn.execute(
            "INSERT INTO team_budgets(league_key, season, team_key, team_name, "
            "faab_balance, waiver_priority, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (LEAGUE, SEASON, team_id, f"Team {team_id}", balance, None, db.utcnow()),
        )
    conn.commit()
    return path


# --- `fcc faab` -------------------------------------------------------------

def test_faab_command_runs_end_to_end(league_db, capsys):
    """The exact failure this file exists for: the command, not the model."""
    code = cli.main(
        ["--db", str(league_db), "faab", "Waiver Add", "--week", str(WEEK), "--budget", "73"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "RECOMMENDED BID" in out
    assert "Waiver Add (RB)" in out
    # An attribute that vanished in the reshape would raise; an empty render
    # would not, so the numbers themselves are asserted on.
    assert "worth to you $" in out and "going rate $" in out


def test_faab_curve_renders(league_db, capsys):
    code = cli.main(
        ["--db", str(league_db), "faab", "Waiver Add", "--week", str(WEEK), "--curve"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "win probability by bid" in out
    assert "<- recommended" in out


def _config_without_team(tmp_path):
    """A config with no my_team_id.

    Written out rather than relying on the repository's config.yaml, which now
    has a real team id in it - a test must not depend on a setting the user is
    expected to fill in, in either direction.
    """
    path = tmp_path / "noteam.yaml"
    path.write_text(
        "league:\n"
        '  league_id: "796511"\n'
        "  season: 2026\n"
        "paths:\n"
        f'  env_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    return str(path)


def _config(tmp_path):
    """A config naming team 3 as mine, which several behaviours depend on."""
    path = tmp_path / "with_team.yaml"
    path.write_text(
        "league:\n"
        '  league_id: "796511"\n'
        "  season: 2026\n"
        "  my_team_id: 3\n"
        "paths:\n"
        f'  env_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    return str(path)


def test_faab_uses_the_synced_budget_when_no_flag_is_given(league_db, tmp_path, capsys):
    """team_budgets is the point of the sync; the command must actually read it."""
    cli.main(["--config", _config(tmp_path), "--db", str(league_db),
              "faab", "Waiver Add", "--week", str(WEEK)])
    out = capsys.readouterr().out
    assert "your budget" in out and "$73" in out, out


def test_faab_never_lists_you_among_your_own_rivals(league_db, tmp_path, capsys):
    """With my_team_id set, your own profile must be out of the field.

    Left in, the model prices the player against a field that contains you and
    names you as a likely rival - so you end up bidding against yourself.
    """
    cli.main(["--config", _config(tmp_path), "--db", str(league_db),
              "faab", "Waiver Add", "--week", str(WEEK)])
    out = capsys.readouterr().out
    rivals = [line for line in out.splitlines() if "likely rivals" in line]
    assert not any("Butt Fumblers" in line for line in rivals), rivals


def test_faab_warns_when_it_cannot_tell_which_team_is_yours(league_db, tmp_path, capsys):
    """Silently counting yourself as a rival is worse than saying you cannot tell."""
    # Its own config with no team id - the repository config now has one, and a
    # test must not depend on a setting the user is expected to fill in.
    config = _config_without_team(tmp_path)
    code = cli.main(["--config", str(config), "--db", str(league_db),
                     "faab", "Waiver Add", "--week", str(WEEK)])
    out = capsys.readouterr().out
    assert code == 0
    assert "my_team_id is not set" in out


def test_faab_survives_an_unreadable_budget_string(league_db, capsys):
    code = cli.main(
        ["--db", str(league_db), "faab", "Waiver Add", "--week", str(WEEK),
         "--budgets", "1:forty,2:11"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Ignoring unreadable budget entry" in out
    assert "RECOMMENDED BID" in out


def test_faab_reports_an_unknown_player_rather_than_raising(league_db, capsys):
    code = cli.main(["--db", str(league_db), "faab", "Nobody At All", "--week", str(WEEK)])
    out = capsys.readouterr().out
    assert code != 0
    assert "No player called" in out


# --- the waiver job's FAAB branch -------------------------------------------

def test_waiver_run_produces_bids_without_crashing(league_db):
    """`waivers.run` reaches the same model through a different door."""
    from src.season import waivers

    conn = db.init_db(league_db)
    report = waivers.run(
        conn, LEAGUE, MY_TEAM, SEASON, WEEK,
        uses_faab=True, budget_left=73, value_margin=8.0,
    )
    assert report.claims, "the free agent is worth far more than the worst bench spot"
    claim = report.claims[0]
    assert claim.bid_rec is not None and claim.bid_rec > 0
    # min <= recommended <= max, or the three numbers are not describing one bid.
    assert claim.bid_min <= claim.bid_rec <= claim.bid_max

    notification = waivers.to_notification(report, SEASON)
    assert notification is not None
    assert "Waiver Add" in notification.text()


def test_waiver_bids_respect_a_budget_that_is_nearly_spent(league_db):
    from src.season import waivers

    conn = db.init_db(league_db)
    report = waivers.run(
        conn, LEAGUE, MY_TEAM, SEASON, WEEK,
        uses_faab=True, budget_left=4, value_margin=8.0,
    )
    for claim in report.claims:
        assert claim.bid_rec <= 4, "recommended a bid the manager cannot make"


# --- configuration that decides where writes land ---------------------------

def test_an_explicit_db_path_never_opens_postgres(tmp_path):
    """`--db` names a file, so it must mean SQLite even with DATABASE_URL set.

    The conftest guard makes `connect_postgres` raise, so reaching Postgres here
    fails loudly rather than connecting to the live league - which is exactly
    what happened before this flag took precedence.
    """
    from src import storage

    conn = storage.connect(
        tmp_path / "explicit.db",
        url="postgresql://user:pw@example.invalid:5432/postgres",
        force_sqlite=True,
    )
    assert conn.dialect == "sqlite"


def test_a_commented_out_credential_is_not_configured(tmp_path, monkeypatch):
    """The shipped .env carries commented placeholders; they are not credentials."""
    from src.cli import Context

    monkeypatch.delenv("YAHOO_CONSUMER_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "# YAHOO_CONSUMER_KEY=put-yours-here\nDATABASE_URL=sqlite\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        f'paths:\n  env_dir: "{tmp_path.as_posix()}"\nleague:\n  league_id: "1"\n'
        "  season: 2026\n",
        encoding="utf-8",
    )
    ctx = Context(str(tmp_path / "config.yaml"), str(tmp_path / "t.db"))
    assert ctx.yahoo_configured() is False

    (tmp_path / ".env").write_text(
        "YAHOO_CONSUMER_KEY=a-real-looking-key\n", encoding="utf-8"
    )
    ctx = Context(str(tmp_path / "config.yaml"), str(tmp_path / "t.db"))
    assert ctx.yahoo_configured() is True


def test_sync_league_refuses_rather_than_authenticating_with_nothing(tmp_path, capsys):
    """No credentials must mean a clean refusal, not an OAuth prompt in a cron job."""
    from src import cli

    (tmp_path / ".env").write_text("# YAHOO_CONSUMER_KEY=\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        f'paths:\n  env_dir: "{tmp_path.as_posix()}"\nleague:\n  league_id: "1"\n'
        "  season: 2026\n",
        encoding="utf-8",
    )
    code = cli.main(
        ["--config", str(tmp_path / "config.yaml"), "--db", str(tmp_path / "t.db"),
         "sync-league", "--week", "5"]
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "not configured" in out


# --- what a waiver claim is actually worth ----------------------------------

SLOTS = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 2, "DEF": 1}


@pytest.fixture
def lineup_league(tmp_path):
    """A full starting lineup, one defence, and a pool of free agents."""
    path = tmp_path / "lineup.db"
    conn = db.init_db(path)

    roster = [
        ("qb1|QB", "Starting QB", "QB", 320.0),
        ("rb1|RB", "Starting RB1", "RB", 240.0),
        ("rb2|RB", "Starting RB2", "RB", 230.0),
        ("wr1|WR", "Starting WR1", "WR", 200.0),
        ("wr2|WR", "Starting WR2", "WR", 190.0),
        ("wr3|WR", "Flex WR3", "WR", 180.0),
        ("rb3|RB", "Flex RB3", "RB", 175.0),
        ("te1|TE", "Starting TE", "TE", 150.0),
        ("def1|DEF", "The Only Defence", "DEF", 130.0),
        ("te2|TE", "Spare TE", "TE", 120.0),
    ]
    for key, name, pos, pts in roster:
        _player(conn, key, name, pos, pts)
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
            "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (LEAGUE, MY_TEAM, "Butt Fumblers", key, pos, WEEK, db.utcnow()),
        )

    free_agents = [
        ("faqb|QB", "Excellent Backup QB", "QB", 310.0),   # never starts: 1 QB slot
        ("fawr|WR", "Genuine Upgrade WR", "WR", 235.0),    # displaces Flex RB3
        ("fadef|DEF", "Spare Defence", "DEF", 125.0),
    ]
    for key, name, pos, pts in free_agents:
        _player(conn, key, name, pos, pts)
        conn.execute(
            "INSERT INTO free_agents(league_key, player_key, pct_owned, week, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (LEAGUE, key, 30.0, WEEK, db.utcnow()),
        )
    conn.commit()
    return path


def test_a_backup_at_a_single_slot_position_is_worth_nothing(lineup_league):
    """A second QB in a one-QB league cannot start, so it is not an upgrade.

    Valuing claims against the worst player on the roster instead of against the
    starting lineup recommended four backup quarterbacks in a row: each beat the
    worst bench spot easily, and not one of them would ever have played.
    """
    from src.season import waivers

    conn = db.init_db(lineup_league)
    report = waivers.run(
        conn, LEAGUE, MY_TEAM, SEASON, WEEK,
        uses_faab=True, budget_left=100, value_margin=1.0, starting_slots=SLOTS,
    )
    names = [c.add.name for c in report.claims]
    assert "Excellent Backup QB" not in names
    assert "Genuine Upgrade WR" in names, names


def test_the_only_defence_is_never_a_drop_candidate(lineup_league):
    """Dropping the last player at a required position forfeits that slot."""
    from src.season import waivers

    conn = db.init_db(lineup_league)
    report = waivers.run(
        conn, LEAGUE, MY_TEAM, SEASON, WEEK,
        uses_faab=True, budget_left=100, value_margin=1.0, starting_slots=SLOTS,
    )
    dropped = {c.drop.name for c in report.claims if c.drop}
    assert "The Only Defence" not in dropped, dropped


def test_a_claim_gain_is_the_lineup_improvement(lineup_league):
    """The number in the notification has to be the number that matters."""
    from src.season import waivers

    conn = db.init_db(lineup_league)
    report = waivers.run(
        conn, LEAGUE, MY_TEAM, SEASON, WEEK,
        uses_faab=True, budget_left=100, value_margin=1.0, starting_slots=SLOTS,
    )
    upgrade = next(c for c in report.claims if c.add.name == "Genuine Upgrade WR")
    # 235 in, Flex RB3 (175) out of the lineup; the dropped spare TE never started.
    assert upgrade.value_gain == pytest.approx(60.0, abs=0.5)


# --- the lineup job must not report a confident zero ------------------------

def test_lineup_says_so_when_the_week_has_no_projections(lineup_league):
    """Silence beats "0 changes suggested" when every projection is missing."""
    from src.season import lineup

    conn = db.init_db(lineup_league)
    report = lineup.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, SLOTS)
    assert report.has_data is False
    assert report.roster_size == 10 and report.projected == 0

    notification = lineup.to_notification(report, SEASON)
    assert notification is not None
    assert "no data" in notification.title
    assert "change(s) suggested" not in notification.title


# --- features that were written, tested, and connected to nothing -----------

def test_prior_season_flags_reach_the_board(tmp_path):
    """priors.py was imported by no production module at all.

    It had unit tests and a measured persistence coefficient and produced
    nothing, because nothing ever called it. This pins the wiring, not the
    maths - the maths has its own tests.
    """
    from src import vorp

    conn = db.init_db(tmp_path / "priors.db")
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 2, "DEF": 1}

    # Two receivers alike on paper; one beat his usage badly last season.
    for key, name, pts in (("hot|WR", "Ran Hot", 200.0), ("cold|WR", "Ran Cold", 200.0)):
        _player(conn, key, name, "WR", pts)
    for i in range(20):
        _player(conn, f"filler{i}|WR", f"Filler {i}", "WR", 150.0 - i)

    # A prior season: expected points from usage, actual points scored.
    for week in range(1, 13):
        rows = [("hot|WR", 10.0, 22.0), ("cold|WR", 10.0, 2.0)]
        rows += [(f"filler{i}|WR", 10.0, 10.0) for i in range(20)]
        for key, expected, actual in rows:
            conn.execute(
                "INSERT INTO player_week_usage(player_key, season, week, "
                "points_expected, points_actual, recorded_at) VALUES (?,?,?,?,?,?)",
                (key, SEASON - 1, week, expected, actual, db.utcnow()),
            )
    conn.commit()

    board = vorp.build_board(conn, SEASON, slots, 12)
    by_name = {p.name: p for p in board.players}
    assert by_name["Ran Hot"].prior_verdict == "inflated", by_name["Ran Hot"].prior_verdict
    assert by_name["Ran Cold"].prior_verdict == "deflated"
    assert by_name["Filler 0"].prior_verdict is None

    # Flagging alone must not move the numbers.
    assert by_name["Ran Hot"].points == 200.0
    assert by_name["Ran Hot"].prior_adjustment == 0.0

    # Acting on it is opt-in, and it discounts rather than inflates.
    adjusted = vorp.build_board(conn, SEASON, slots, 12, prior_strength=0.5)
    hot = next(p for p in adjusted.players if p.name == "Ran Hot")
    cold = next(p for p in adjusted.players if p.name == "Ran Cold")
    assert hot.points < 200.0, "an inflated season should be discounted"
    assert cold.points > 200.0, "a deflated season should be credited back"


def test_a_board_without_prior_season_data_is_unchanged(tmp_path):
    """The normal state before `fcc sync-usage` has ever run."""
    from src import vorp

    conn = db.init_db(tmp_path / "nopriors.db")
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 2, "DEF": 1}
    for i in range(6):
        _player(conn, f"wr{i}|WR", f"Receiver {i}", "WR", 200.0 - i * 10)
    conn.commit()

    board = vorp.build_board(conn, SEASON, slots, 12, prior_strength=0.5)
    assert board.players, "the board must still build"
    assert all(p.prior_verdict is None for p in board.players)
    assert all(p.prior_adjustment == 0.0 for p in board.players)


def test_the_daily_run_syncs_the_table_its_analytics_read():
    """player_week_usage is filled by exactly one command, which nothing called.

    Every feature built on it - prior-season flags, expected-points regression,
    measured volatility - was permanently dark in a scheduled deployment.
    """
    import inspect

    from src import cli

    source = inspect.getsource(cli.cmd_daily)
    assert "cmd_sync_usage" in source, "fcc daily must populate player_week_usage"


# --- no job may raise an alarm about missing data ---------------------------

def test_the_bye_planner_does_not_alarm_about_an_empty_roster(tmp_path):
    """With no players, every week is unfillable - which is not four problems."""
    from src.season import byes

    conn = db.init_db(tmp_path / "nobody.db")
    report = byes.run(conn, LEAGUE, MY_TEAM, SEASON, 3, SLOTS)
    assert report.has_data is False
    assert report.roster_size == 0

    notification = byes.to_notification(report, SEASON)
    assert notification is not None
    assert "no roster" in notification.title.lower()


def test_the_recap_of_an_unplayed_week_is_not_a_zero_point_recap(tmp_path):
    from src.season import recap

    conn = db.init_db(tmp_path / "unplayed.db")
    report = recap.run(conn, LEAGUE, MY_TEAM, SEASON, 3, SLOTS)
    assert report.has_data is False
    notification = recap.to_notification(report, SEASON)
    assert notification is not None
    assert "no scores" in notification.title.lower()


def test_no_command_raises_against_an_empty_database(tmp_path, capsys):
    """A first run on a new machine, and the state right after a migration.

    Not one of these should traceback; every one should say what it needs.
    """
    config = tmp_path / "empty.yaml"
    config.write_text(
        "league:\n"
        '  league_id: "796511"\n'
        "  season: 2026\n"
        "  my_team_id: 3\n"
        "paths:\n"
        f'  env_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    database = tmp_path / "empty.db"

    commands = [
        ["rank"],
        ["draft", "--slot", "3", "--rounds", "3", "--offline"],
        ["injuries", "--week", "3", "--dry-run"],
        ["lineup", "--week", "3", "--dry-run"],
        ["waivers", "--week", "3", "--dry-run"],
        ["byes", "--week", "3", "--dry-run"],
        ["recap", "--week", "3", "--dry-run"],
        ["trades", "--week", "3", "--dry-run"],
        ["reminders", "--week", "3", "--dry-run"],
        ["regression", "--season", "2026", "--week", "3"],
        ["accuracy", "--season", "2026", "--week", "3"],
        ["startsit", "--week", "3"],
        ["playoffs", "--week", "3", "--trials", "50"],
        ["faab", "--week", "3"],
    ]
    for argv in commands:
        code = cli.main(["--config", str(config), "--db", str(database)] + argv)
        out = capsys.readouterr().out
        assert code == 0, f"fcc {' '.join(argv)} exited {code}: {out}"
        assert "Traceback" not in out, f"fcc {' '.join(argv)}:\n{out}"
        assert out.strip(), f"fcc {' '.join(argv)} said nothing at all"


# --- the trade scout --------------------------------------------------------

@pytest.fixture
def lopsided_league(tmp_path):
    """Me RB-rich and WR-poor; one rival the exact mirror image."""
    path = tmp_path / "trades.db"
    conn = db.init_db(path)

    def add(team, key, name, pos, pts):
        if not conn.fetchone("SELECT 1 FROM players WHERE player_key=?", (key,)):
            _player(conn, key, name, pos, pts)
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
            "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (LEAGUE, team, f"Team {team}", key, pos, WEEK, db.utcnow()),
        )

    for i, pts in enumerate([260, 250, 240, 230, 220]):
        add(MY_TEAM, f"myrb{i}|RB", f"My RB{i}", "RB", pts)
    for i, pts in enumerate([120, 110]):
        add(MY_TEAM, f"mywr{i}|WR", f"My WR{i}", "WR", pts)
    for i, pts in enumerate([230, 220, 210, 200, 190]):
        add("9", f"thwr{i}|WR", f"Their WR{i}", "WR", pts)
    for i, pts in enumerate([115, 105]):
        add("9", f"thrb{i}|RB", f"Their RB{i}", "RB", pts)
    for team in (MY_TEAM, "9"):
        add(team, f"qb{team}|QB", f"QB {team}", "QB", 300.0)
        add(team, f"te{team}|TE", f"TE {team}", "TE", 150.0)
        add(team, f"def{team}|DEF", f"DEF {team}", "DEF", 110.0)
    conn.commit()
    return path


def test_the_trade_scout_finds_an_obvious_swap(lopsided_league):
    from src.season import trades

    conn = db.init_db(lopsided_league)
    ideas = trades.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, SLOTS)
    assert ideas, "an RB-rich team and a WR-rich team should have a trade"
    idea = ideas[0]
    assert idea.i_give[0].position == "RB"
    assert idea.i_get[0].position == "WR"
    assert idea.my_gain > 0 and idea.their_gain > 0


def test_a_trade_rationale_is_checked_against_the_rosters(lopsided_league):
    """It used to assert "you are deep at X" without ever looking.

    Here it happens to be true, so the derived sentence must quote the actual
    surplus rather than the position name alone.
    """
    from src.season import trades

    conn = db.init_db(lopsided_league)
    idea = trades.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, SLOTS)[0]
    text = " ".join(idea.rationale)
    assert "pts of RB" in text, text
    assert "lineup effect" in text


def test_a_balanced_league_yields_no_trades(lineup_league):
    """No idea is the right answer more often than not; it must not invent one."""
    from src.season import trades

    conn = db.init_db(lineup_league)
    assert trades.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, SLOTS) == []


# --- the draft has to know whose pick each one was --------------------------

def test_picks_are_attributed_by_snake_order_not_by_who_pressed(tmp_path):
    """The dashboard's Draft button used to record every pick to my team.

    Pressed out of turn it added another team's player to my roster, and since
    the pick counter still advanced, every pick after it was attributed to the
    wrong team too. Recording without a team key lets the snake order decide,
    which is the only thing that can be right.
    """
    from src.draft.live import DraftTracker

    conn = db.init_db(tmp_path / "draft.db")
    for i in range(30):
        _player(conn, f"d{i}|RB", f"Player {i}", "RB", 200.0 - i)
    conn.commit()

    tracker = DraftTracker(conn, LEAGUE, 12, 14, None)
    for i in range(14):
        tracker.record_pick(f"d{i}|RB")

    picks = tracker.state.picks
    # Round 1 runs 1..12 in order; round 2 snakes back, so pick 13 is slot 12.
    assert str(picks[1].team_key) == "1"
    assert str(picks[3].team_key) == "3"
    assert str(picks[12].team_key) == "12"
    assert str(picks[13].team_key) == "12"
    assert str(picks[14].team_key) == "11"

    # And my roster contains only the picks the order says are mine.
    mine = set(tracker.state.roster_of("3"))
    assert mine == {"d2|RB"}, mine


def test_undo_puts_a_player_back_on_the_board(tmp_path):
    """Mis-taps happen at ninety seconds a pick."""
    from src.draft.live import DraftTracker

    conn = db.init_db(tmp_path / "undo.db")
    for i in range(5):
        _player(conn, f"u{i}|RB", f"Player {i}", "RB", 200.0 - i)
    conn.commit()

    tracker = DraftTracker(conn, LEAGUE, 12, 14, None)
    tracker.record_pick("u0|RB")
    tracker.record_pick("u1|RB")
    assert tracker.state.next_pick == 3

    removed = tracker.undo_last()
    assert removed is not None and removed.player_key == "u1|RB"
    assert "u1|RB" not in tracker.state.drafted_keys
    assert tracker.state.next_pick == 2


# --- a report nobody received must not be consumed --------------------------

def test_an_injury_report_survives_a_failed_delivery(tmp_path):
    """The baseline used to advance inside `run`, before anything was sent.

    A re-run, an SMTP failure, or the daily path running injuries twice threw
    the alert away for good - and `--dry-run` did the same, so an injury report
    could not be previewed without destroying it.
    """
    from src.season import injuries

    conn = db.init_db(tmp_path / "injuries.db")
    _player(conn, "hurt|RB", "Hurt Player", "RB", 200.0)
    conn.execute(
        "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
        "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (LEAGUE, MY_TEAM, "Butt Fumblers", "hurt|RB", "RB", WEEK, db.utcnow()),
    )

    def record(status, when):
        conn.execute(
            "INSERT INTO injuries(player_key, status, source, observed_at) "
            "VALUES (?,?,?,?)",
            ("hurt|RB", status, "sleeper", when),
        )
        conn.commit()

    record("Questionable", "2026-10-01T12:00:00+00:00")
    baseline = injuries.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK)
    assert baseline.first_run is True
    injuries.commit(conn, baseline)

    record("Out", "2026-10-02T12:00:00+00:00")

    # Read it twice without committing: the delta must still be there.
    first = injuries.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK)
    assert [c.player_key for c in first.actionable] == ["hurt|RB"]

    second = injuries.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK)
    assert [c.player_key for c in second.actionable] == ["hurt|RB"], (
        "reading the report must not consume it"
    )

    # Only after an explicit commit does it stop being news.
    injuries.commit(conn, second)
    third = injuries.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK)
    assert third.actionable == []


def test_a_roster_less_job_fails_rather_than_reporting_success(tmp_path, capsys):
    """Six of seven scheduled jobs used to no-op and leave a green run behind."""
    config = tmp_path / "noteam.yaml"
    config.write_text(
        "league:\n"
        '  league_id: "796511"\n'
        "  season: 2026\n"
        "paths:\n"
        f'  env_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    for job in ("waivers", "lineup", "byes", "recap", "trades", "injuries"):
        code = cli.main(
            ["--config", str(config), "--db", str(tmp_path / "x.db"),
             job, "--week", "5", "--dry-run"]
        )
        out = capsys.readouterr().out
        assert code != 0, f"fcc {job} reported success with no team configured"
        assert "my_team_id is not set" in out


# --- lineup advice must be executable in Yahoo ------------------------------

def _roster_db(tmp_path, name, players, slots_week=WEEK):
    """players: (key, name, position, points, selected_pos, injury, bye)."""
    conn = db.init_db(tmp_path / name)
    for key, label, pos, pts, slot, injury, bye in players:
        conn.execute(
            "INSERT INTO players(player_key, full_name, position, team, bye_week, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            (key, label, pos, "NYJ", bye, db.utcnow()),
        )
        conn.execute(
            "INSERT INTO projections_blended(player_key, season, week, points, "
            "computed_at) VALUES (?,?,?,?,?)",
            (key, SEASON, slots_week, pts, db.utcnow()),
        )
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, player_key, selected_pos, "
            "week, fetched_at) VALUES (?,?,?,?,?,?)",
            (LEAGUE, MY_TEAM, key, slot, slots_week, db.utcnow()),
        )
        if injury:
            conn.execute(
                "INSERT INTO injuries(player_key, status, source, observed_at) "
                "VALUES (?,?,?,?)",
                (key, injury, "sleeper", "2026-10-01T00:00:00+00:00"),
            )
    conn.commit()
    return conn


def test_a_swap_names_a_player_who_could_hold_that_slot(tmp_path):
    """It used to pick the globally worst displaced starter, ignoring the slot.

    That produced instructions no manager can execute - "QB: START QBGood over
    WRBad" - and stated gains that were wrong in both directions.
    """
    from src.season import lineup

    conn = _roster_db(tmp_path, "swaps.db", [
        ("qbbad|QB", "QBBad", "QB", 5.0, "QB", None, None),
        ("wrbad|WR", "WRBad", "WR", 9.0, "WR", None, None),
        ("qbgood|QB", "QBGood", "QB", 25.0, "BN", None, None),
        ("wrgood|WR", "WRGood", "WR", 22.0, "BN", None, None),
    ])
    report = lineup.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, {"WR": 1, "QB": 1})
    by_slot = {s.slot: s for s in report.swaps}
    assert by_slot["QB"].starter_out.position == "QB", by_slot["QB"].describe()
    assert by_slot["WR"].starter_out.position == "WR", by_slot["WR"].describe()
    assert by_slot["QB"].gain == pytest.approx(20.0)
    assert by_slot["WR"].gain == pytest.approx(13.0)


def test_an_unfillable_week_is_reported_not_hidden(tmp_path):
    """An Out player used to be assigned anyway, so is_complete stayed True."""
    from src.season import lineup

    conn = _roster_db(tmp_path, "short.db", [
        ("sq|QB", "Starter QB", "QB", 20.0, "QB", None, None),
        ("it|TE", "Injured TE", "TE", 8.0, "TE", "Out", None),
    ])
    report = lineup.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, {"QB": 1, "TE": 1})
    assert report.optimal.is_complete is False
    assert report.optimal.empty_slots == ["TE"]
    assert any("Cannot fill TE" in w for w in report.warnings), report.warnings


def test_a_bye_player_parked_on_ir_plus_is_not_called_a_starter(tmp_path):
    """Two different bench-slot lists disagreed about IR+ and NA."""
    from src.season import lineup

    conn = _roster_db(tmp_path, "irplus.db", [
        ("ok|QB", "Fine QB", "QB", 20.0, "QB", None, None),
        ("bye|WR", "Bye Guy", "WR", 15.0, "IR+", None, WEEK),
    ])
    report = lineup.run(conn, LEAGUE, MY_TEAM, SEASON, WEEK, {"QB": 1, "WR": 1})
    assert not any("Bye Guy is starting" in w for w in report.warnings), report.warnings


# --- scoring rules ----------------------------------------------------------

def test_fractional_points_allowed_lands_in_a_bucket():
    """Yahoo's brackets are integers; projections are not.

    A literal low <= value <= high left a gap between every pair of brackets,
    so 20.5 points allowed matched nothing and scored zero - silently, on the
    largest component of a defence's score, eighteen times a season.
    """
    from src import league_bootstrap, scoring

    rules = scoring.build_from_yahoo(league_bootstrap.build_settings())
    buckets = [c for c in rules.categories if c.is_bucket]
    assert buckets, "this league should score points allowed in brackets"

    for value in (0.0, 0.9, 3.0, 6.5, 13.4, 16.5, 20.5, 27.0, 28.0, 34.6, 35.0, 41.2):
        matched = [c for c in buckets if c.matches_bucket(value)]
        assert len(matched) == 1, (
            f"points allowed {value} matched {[c.display_name for c in matched]}"
        )


def test_passing_two_point_conversions_score():
    """They mapped to a canonical no scoring category produces, so were free."""
    from src import league_bootstrap, scoring
    from src.sources.sleeper_projections import STAT_MAP

    rules = scoring.build_from_yahoo(league_bootstrap.build_settings())
    passing = rules.score({STAT_MAP["pass_2pt"]: 2.0})
    rushing = rules.score({STAT_MAP["rush_2pt"]: 2.0})
    assert passing > 0, "a passing 2-point conversion scored nothing"
    assert passing == rushing, "Yahoo scores one combined 2-point category"


# --- the recommender must never prefer a worse player -----------------------

def test_score_is_monotone_in_vorp_at_every_need_level():
    """`base * (2.0 - need)` inverted above need=2.0, which the deferred-position
    path reaches in the final round: a kicker at VORP -30 scored +15 and ranked
    first, ahead of one at -5, in the round that forces you to draft one."""
    from src.draft.recommender import DraftRecommender
    from src.draft.survival import DraftPosition
    from src.vorp import Board

    rec = DraftRecommender(Board(players=[]), DraftPosition(12, 3, rounds=14))
    for need in (0.25, 1.0, 2.0, 2.5, 4.0):
        scored = [(v, rec._score_for(v, need, 1.0)) for v in (-40.0, -5.0, 5.0, 60.0)]
        ordered = [v for v, _ in sorted(scored, key=lambda kv: -kv[1])]
        assert ordered == sorted([v for v, _ in scored], reverse=True), (
            f"need={need} ranked {ordered}"
        )


# --- the draft must never lock you out --------------------------------------

def test_falling_behind_the_real_draft_is_recoverable(tmp_path):
    """The live-draft failure the pick counter used to cause.

    `next_pick` was derived purely from how many picks had been typed in, and
    it gated the draft controls. Missing five opponent picks meant arriving at
    your own turn, on a 90-second clock, with every button disabled and a
    message telling you to go do data entry first.
    """
    from src.draft.live import DraftTracker

    conn = db.init_db(tmp_path / "behind.db")
    for i in range(40):
        _player(conn, f"p{i}|RB", f"Player {i}", "RB", 200.0 - i)
    conn.commit()

    tracker = DraftTracker(conn, LEAGUE, 12, 14, None)
    tracker.record_pick("p0|RB")
    tracker.record_pick("p1|RB")
    assert tracker.state.next_pick == 3

    # Slot 3's second pick is 22. Jump straight there.
    written = tracker.skip_to(22)
    assert written == 19
    assert tracker.state.next_pick == 22
    assert tracker.state.team_key_for_pick(22) == 3

    # The players nobody named are still on the board - a missed pick costs
    # only that player, not the board.
    assert tracker.state.drafted_keys == {"p0|RB", "p1|RB"}

    tracker.record_pick("p5|RB", team_key="3")
    assert tracker.state.roster_of("3") == ["p5|RB"]


def test_an_unknown_pick_advances_the_count_without_claiming_a_player(tmp_path):
    from src.draft.live import DraftTracker

    conn = db.init_db(tmp_path / "unknown.db")
    _player(conn, "seen|RB", "Seen", "RB", 200.0)
    conn.commit()

    tracker = DraftTracker(conn, LEAGUE, 12, 14, None)
    tracker.record_pick(None, source="skipped")
    assert tracker.state.next_pick == 2
    assert tracker.state.drafted_keys == set()

    tracker.record_pick("seen|RB")
    assert tracker.state.drafted_keys == {"seen|RB"}

    # And an anonymous pick can be undone like any other.
    tracker.undo_last()
    tracker.undo_last()
    assert tracker.state.next_pick == 1


def test_the_draft_controls_are_never_disabled(tmp_path, monkeypatch):
    """A control that disappears mid-draft is worse than one that is wrong."""

    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DATABASE_URL", "")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard.py"),
                            default_timeout=180)
    app.run()
    assert not app.exception, [str(e.value) for e in app.exception]

    record_buttons = [
        b for b in app.button
        if b.label.startswith(("Take", "Gone", "Didn't catch"))
    ]
    assert record_buttons, [b.label for b in app.button]
    assert not any(b.disabled for b in record_buttons), (
        [(b.label, b.disabled) for b in record_buttons]
    )

    # And the pick number is settable rather than purely derived.
    assert any(n.label == "Pick" for n in app.number_input)


# --- a job that does nothing must still leave evidence ----------------------

def test_every_run_records_a_row(tmp_path, capsys):
    """The failure mode here is not a crash, it is silence.

    A job that runs, decides nothing, sends nothing and exits 0 looks exactly
    like a healthy quiet week - and exactly like one that has been broken for a
    month, which is what six of the seven scheduled jobs actually were. So a
    row is written whatever the outcome.
    """
    from src import db as dbm

    database = tmp_path / "runs.db"
    config = tmp_path / "c.yaml"
    config.write_text(
        "league:\n"
        '  league_id: "796511"\n'
        "  season: 2026\n"
        "  my_team_id: 3\n"
        "paths:\n"
        f'  env_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )

    cli.main(["--config", str(config), "--db", str(database), "rank"])
    capsys.readouterr()
    cli.main(["--config", str(config), "--db", str(database),
              "waivers", "--week", "3", "--dry-run"])
    capsys.readouterr()

    conn = dbm.init_db(database)
    rows = conn.fetchall("SELECT job, status, exit_code FROM job_runs ORDER BY id")
    jobs = [r["job"] for r in rows]
    assert "rank" in jobs and "waivers" in jobs, jobs
    for row in rows:
        assert row["status"] in ("ok", "failed")
        assert row["exit_code"] is not None

    health = dbm.job_health(conn, days=10)
    assert {h["job"] for h in health} >= {"rank", "waivers"}
    assert all(h["runs"] >= 1 for h in health)


def test_a_failing_job_is_recorded_as_failed(tmp_path, capsys):
    """`fcc doctor` has to be able to say which jobs are not working."""
    from src import db as dbm

    database = tmp_path / "fail.db"
    config = tmp_path / "noteam.yaml"
    config.write_text(
        "league:\n"
        '  league_id: "796511"\n'
        "  season: 2026\n"
        "paths:\n"
        f'  env_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    code = cli.main(["--config", str(config), "--db", str(database),
                     "lineup", "--week", "3", "--dry-run"])
    capsys.readouterr()
    assert code != 0

    conn = dbm.init_db(database)
    row = conn.fetchone(
        "SELECT status, exit_code FROM job_runs WHERE job='lineup' ORDER BY id DESC"
    )
    assert row is not None and row["status"] == "failed"
