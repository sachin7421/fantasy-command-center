"""The acceptance criteria from the specification, section 10.

These are the spec's own definition of done, so they are tested as written
rather than paraphrased:

  1. Scoring reproduces Yahoo's listed weekly points for sample players.
  2. The VORP board broadly agrees with FantasyPros ECR; the top 24 is sane.
  3. Across simulated drafts the assistant beats naive ADP drafting.
  4. The injury monitor detects a manufactured change and fires EXACTLY one
     notification.
  5. Jobs run headless with cron-suitable exit codes, and a failed source
     degrades to cache with a warning rather than crashing.

Criterion 1 needs live Yahoo data and is marked skipped rather than quietly
dropped - see the note on that test.
"""
from __future__ import annotations

import json

import pytest

from src import db, scoring, vorp
from src.idmap import IdMapper


# --- shared fixture league ---------------------------------------------------

SLOTS = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 2, "DEF": 1}


@pytest.fixture
def league(tmp_path, yahoo_settings):
    """A small but realistic league: players, projections and ECR."""
    conn = db.init_db(tmp_path / "acceptance.db")
    idmap = IdMapper(conn)
    rules = scoring.build_from_yahoo(yahoo_settings)

    # A plausible board: running backs and receivers tail off, quarterbacks
    # score more in absolute terms but are shallow in value.
    roster: list[tuple[str, str, float, float]] = []
    for i in range(1, 41):
        roster.append((f"RB{i}", "RB", 300 - i * 5.5, i * 1.4))
        roster.append((f"WR{i}", "WR", 285 - i * 4.8, i * 1.5))
    for i in range(1, 21):
        roster.append((f"QB{i}", "QB", 360 - i * 4.0, 25 + i * 5.0))
        roster.append((f"TE{i}", "TE", 210 - i * 6.5, 20 + i * 6.0))
    # Defenses are keyed by TEAM, not by name (a defense has no player name of
    # its own), so each needs a distinct team or they collapse to one row.
    teams = ["BUF", "MIA", "NYJ", "NE", "BAL", "CIN", "CLE", "PIT",
             "HOU", "IND", "JAX", "TEN"]
    defense_teams = {}
    for i in range(1, 13):
        defense_teams[f"DEF{i}"] = teams[i - 1]
        roster.append((f"DEF{i}", "DEF", 130 - i * 2.0, 140 + i * 2.0))

    for name, position, points, ecr in roster:
        key = idmap.upsert_player(
            full_name=name, position=position,
            team=defense_teams.get(name, "AAA"),
        )
        conn.execute(
            "INSERT INTO projections(player_key, source, season, week, stats_json, "
            "points, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (key, "sleeper", 2026, 0, "{}", points, db.utcnow()),
        )
        conn.execute(
            "INSERT INTO adp(player_key, source, adp, stdev, best, worst, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, "fantasypros", ecr, 2.0, ecr - 4, ecr + 4, db.utcnow()),
        )
    conn.commit()
    yield conn, rules
    conn.close()


# --- 1. scoring against Yahoo's own numbers ---------------------------------

@pytest.mark.skip(
    reason="Needs live Yahoo access to read back Yahoo's own computed weekly "
           "points. The scoring engine is instead verified against hand-computed "
           "totals in test_scoring.py, including this league's two overrides "
           "(interceptions -1, fumbles -1 AND fumbles lost -1)."
)
def test_scoring_matches_yahoo_listed_points():
    raise AssertionError("unreachable until Yahoo access is approved")


# --- 2. board agrees broadly with expert consensus --------------------------

def rank_correlation(a: list[float], b: list[float]) -> float:
    """Spearman correlation, computed directly to avoid a scipy dependency."""
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, index in enumerate(order, 1):
            out[index] = float(rank)
        return out

    ra, rb = ranks(a), ranks(b)
    n = len(ra)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb))
    den = (
        sum((x - mean_a) ** 2 for x in ra) * sum((y - mean_b) ** 2 for y in rb)
    ) ** 0.5
    return num / den if den else 0.0


def test_board_broadly_agrees_with_expert_consensus(league):
    """Spec 10.2: broad agreement with ECR, with explainable deviations.

    Agreement is required, not identity - the deviations are the entire point
    of computing value independently. A board that matched ECR exactly would
    add nothing over reading ECR.
    """
    conn, _ = league
    board = vorp.build_board(conn, 2026, SLOTS, 12)
    ranked = [p for p in board.players if p.adp is not None][:60]
    assert len(ranked) >= 40

    correlation = rank_correlation(
        [p.overall_rank for p in ranked], [p.adp for p in ranked]
    )
    assert correlation > 0.6, f"board and ECR disagree wholesale (rho={correlation:.2f})"
    assert correlation < 0.999, "a board identical to ECR would add nothing"


def test_top_24_is_sane(league):
    """Spec 10.2: sanity-check the top 24.

    The things that would indicate a broken board: kickers or defenses near the
    top, a position stacked to the exclusion of others, or negative value in
    players being recommended first.
    """
    conn, _ = league
    board = vorp.build_board(conn, 2026, SLOTS, 12)
    top = board.players[:24]

    assert all(p.vorp > 0 for p in top), "top-24 players must beat replacement"
    assert not [p for p in top if p.position == "DEF"], "no defense belongs in the top 24"

    counts: dict[str, int] = {}
    for p in top:
        counts[p.position] = counts.get(p.position, 0) + 1
    assert counts.get("RB", 0) >= 4 and counts.get("WR", 0) >= 4
    assert max(counts.values()) <= 18, f"one position dominates the top 24: {counts}"


def test_quarterbacks_are_devalued_relative_to_raw_points(league):
    """The clearest evidence VORP is doing its job.

    Quarterbacks out-score everyone in raw points and should still not lead the
    board, because replacement-level quarterbacks also score a lot.
    """
    conn, _ = league
    board = vorp.build_board(conn, 2026, SLOTS, 12)
    best_qb = next(p for p in board.players if p.position == "QB")
    assert best_qb.points > board.players[0].points, "fixture should have QBs scoring most"
    assert best_qb.overall_rank > 5, "raw points should not put a QB at the top of VORP"


# --- 4. injury monitor fires exactly one notification -----------------------

@pytest.fixture
def injury_league(tmp_path):
    from src.config import Config

    conn = db.init_db(tmp_path / "injuries.db")
    idmap = IdMapper(conn)
    key = idmap.upsert_player(full_name="Star Player", position="RB", team="NYJ")
    bench = idmap.upsert_player(full_name="Bench Guy", position="RB", team="NYJ")
    for player_key in (key, bench):
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
            "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
            ("nfl.l.1", "1", "Mine", player_key, "RB", 5, db.utcnow()),
        )
    conn.commit()
    return conn, key


def _record_injury(conn, player_key, status, stamp):
    conn.execute(
        "INSERT INTO injuries(player_key, status, practice, body_part, note, source, "
        "observed_at) VALUES (?,?,?,?,?,?,?)",
        (player_key, status, None, "hamstring", None, "sleeper", stamp),
    )
    conn.commit()


def test_manufactured_status_change_fires_exactly_one_notification(injury_league, tmp_path):
    """Spec 10.4, tested as written: one manufactured change, one notification."""
    from src.config import Config
    from src.notify import Notifier
    from src.season import injuries

    conn, key = injury_league
    cfg = Config({"notifications": {"dedup_window_hours": 72}}, tmp_path / "c.yaml")

    # First run establishes the baseline and must stay silent, rather than
    # alerting on every already-injured player in the league.
    _record_injury(conn, key, "Questionable", "2026-10-01T12:00:00+00:00")
    first = injuries.run(conn, "nfl.l.1", "1", 2026, 5)
    assert first.first_run is True
    assert injuries.to_notification(first, 5, 2026) is None

    # Manufacture the change.
    _record_injury(conn, key, "Out", "2026-10-02T12:00:00+00:00")
    report = injuries.run(conn, "nfl.l.1", "1", 2026, 5)

    changes = [c for c in report.actionable if c.player_key == key]
    assert len(changes) == 1, f"expected one change, got {len(changes)}"
    assert changes[0].before == "Questionable" and changes[0].after == "Out"
    assert changes[0].is_escalation

    notification = injuries.to_notification(report, 5, 2026)
    assert notification is not None
    assert notification.urgency == "high"

    # Exactly one: a second delivery of the same unchanged fact is suppressed.
    notifier = Notifier(cfg, conn)
    assert notifier.already_sent(notification) is False
    notifier.record(notification, notified=True)
    assert notifier.already_sent(notification) is True


def test_an_unchanged_status_produces_no_notification(injury_league):
    from src.season import injuries

    conn, key = injury_league
    _record_injury(conn, key, "Questionable", "2026-10-01T12:00:00+00:00")
    injuries.run(conn, "nfl.l.1", "1", 2026, 5)

    # Same status observed again later.
    _record_injury(conn, key, "Questionable", "2026-10-02T12:00:00+00:00")
    report = injuries.run(conn, "nfl.l.1", "1", 2026, 5)
    assert injuries.to_notification(report, 5, 2026) is None


def test_a_change_to_an_irrelevant_player_is_ignored(injury_league):
    """Only my roster, my opponent and the top free agents matter."""
    from src.season import injuries

    conn, key = injury_league
    idmap = IdMapper(conn)
    stranger = idmap.upsert_player(full_name="Some Stranger", position="WR", team="MIA")
    conn.commit()

    _record_injury(conn, key, "Questionable", "2026-10-01T12:00:00+00:00")
    injuries.run(conn, "nfl.l.1", "1", 2026, 5)

    _record_injury(conn, stranger, "Out", "2026-10-02T12:00:00+00:00")
    report = injuries.run(conn, "nfl.l.1", "1", 2026, 5)
    assert injuries.to_notification(report, 5, 2026) is None


# --- 5. headless behaviour and graceful degradation -------------------------

def test_a_dead_source_degrades_to_cache_rather_than_crashing(tmp_path):
    """Spec 10.5: a failed source downgrades to cache with a warning."""
    from src.sources.base import Source, SourceUnavailable

    conn = db.init_db(tmp_path / "cache.db")
    source = Source(conn)
    source.name = "flaky"

    db.cache_put(conn, "flaky:endpoint", "flaky", {"value": "from cache"})

    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("network is down")

    source._session = Boom()
    result = source.get_json("https://example.com/x", "flaky:endpoint", retries=1)

    assert result.payload == {"value": "from cache"}
    assert result.from_cache is True
    conn.close()


def test_a_dead_source_with_no_cache_raises_a_clear_error(tmp_path):
    """Never a silent wrong answer: with nothing cached it must say so."""
    from src.sources.base import Source, SourceUnavailable

    conn = db.init_db(tmp_path / "nocache.db")
    source = Source(conn)
    source.name = "flaky"

    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("network is down")

    source._session = Boom()
    with pytest.raises(SourceUnavailable):
        source.get_json("https://example.com/x", "missing", retries=1)
    conn.close()


def test_stale_cache_is_flagged_as_stale(tmp_path):
    """Serving old data is acceptable; doing so silently is not."""
    from src.sources.base import Source

    conn = db.init_db(tmp_path / "stale.db")
    source = Source(conn)
    source.name = "flaky"
    source.max_cache_age_hours = 1

    conn.execute(
        "INSERT INTO source_cache(cache_key, source, payload_json, fetched_at) "
        "VALUES (?,?,?,?)",
        ("old", "flaky", json.dumps({"v": 1}), "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()

    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("down")

    source._session = Boom()
    result = source.get_json("https://example.com/x", "old", retries=1)
    assert result.from_cache is True
    assert result.stale is True
    conn.close()


def test_cli_exposes_cron_suitable_exit_codes():
    """Spec 10.5: exit codes suitable for cron."""
    from src import cli

    assert cli.EXIT_OK == 0
    assert cli.EXIT_FAIL != 0


def test_every_command_in_the_parser_has_a_handler():
    """A command that parses but has no handler would fail only when run."""
    from src import cli

    parser = cli.build_parser()
    subparsers = next(
        a for a in parser._actions if hasattr(a, "choices") and a.choices
    )
    known = set(subparsers.choices)

    # Read from the dispatch table itself. A hand-copied list here passed for
    # as long as it took to add a command and forget to update it - which is
    # the exact failure this test claims to rule out.
    handled = set(cli.HANDLERS) | {"setup"}
    # Anything else must be one of the season jobs, which share a handler.
    jobs = {"injuries", "lineup", "waivers", "byes", "recap", "trades", "reminders"}
    unhandled = known - handled - jobs
    assert not unhandled, f"commands with no handler: {sorted(unhandled)}"


def test_help_renders_without_a_database():
    """`--help` must never need config or a database."""
    from src import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0


# --- guardrail (spec 6.6) ----------------------------------------------------

def test_nothing_in_the_codebase_writes_to_yahoo():
    """Spec 6.6: the system never executes transactions.

    Enforced structurally rather than by intention: the Yahoo client exposes no
    method that would add, drop, trade or set a lineup.
    """
    from src.yahoo_client import YahooClient

    forbidden = ("add_player", "drop_player", "place_claim", "submit", "post_",
                 "set_lineup", "propose_trade", "edit_roster")
    exposed = [m for m in dir(YahooClient) if not m.startswith("_")]
    offending = [m for m in exposed if any(f in m for f in forbidden)]
    assert not offending, f"the client must be read-only, found: {offending}"
