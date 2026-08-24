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


def test_faab_uses_the_synced_budget_when_no_flag_is_given(league_db, capsys):
    """team_budgets is the point of the sync; the command must actually read it."""
    cli.main(["--db", str(league_db), "faab", "Waiver Add", "--week", str(WEEK)])
    out = capsys.readouterr().out
    assert "$73" in out


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
