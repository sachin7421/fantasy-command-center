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


def test_faab_warns_when_it_cannot_tell_which_team_is_yours(league_db, capsys):
    """Silently counting yourself as a rival is worse than saying you cannot tell."""
    code = cli.main(["--db", str(league_db), "faab", "Waiver Add", "--week", str(WEEK)])
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
