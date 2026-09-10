"""The Yahoo API agreement, enforced in code rather than in intention.

Signed 2026-09-10. Five obligations, and four of them are the kind that decay
quietly: somebody adds a caching call, a weight gets tuned back up, a footer is
restyled and the attribution goes with it. So each one is a test.

The boundary being enforced - what counts as "Yahoo-derived":

  Yahoo's, must never touch disk
      API responses: settings, teams, rosters, free agents, transactions,
      draft results, budgets. Yahoo player keys. Anything fetched from the API.

  Ours, may persist
      Projections from Sleeper, ESPN, FantasyPros, nflverse. Our blends, VORP,
      tiers and recommendations. Draft picks WE typed in. Learned FAAB
      coefficients - a shrunk dollars-per-point number from which no Yahoo fact
      is recoverable.

  The user's own, may persist
      League scoring rules and roster slots. Transcribed by hand from the
      league settings page, never fetched from the API, so they are this
      manager's configuration of his own league and live in config.yaml.
"""
from __future__ import annotations

import pathlib

import pytest

from src import db


def test_the_cache_refuses_yahoo(tmp_path):
    """The one structural guard everything else rests on.

    Every Yahoo fetch in the client funnels through `_cached`, which calls
    `cache_put`. Refusing at that single point makes persistence impossible
    rather than merely discouraged - a future caller who has never read the
    agreement still cannot write Yahoo data to disk.
    """
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    try:
        with pytest.raises(ValueError, match="Yahoo"):
            db.cache_put(conn, "yahoo:settings:nfl.l.796511", "yahoo", {"a": 1})
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0
    finally:
        conn.close()


@pytest.mark.parametrize("source", ["yahoo", "YAHOO", "Yahoo", "yahoo_fantasy"])
def test_the_refusal_is_not_case_or_suffix_sensitive(tmp_path, source):
    """A guard that a different spelling walks past is not a guard."""
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    try:
        with pytest.raises(ValueError):
            db.cache_put(conn, f"{source}:x", source, {"a": 1})
    finally:
        conn.close()


def test_other_sources_still_cache_normally(tmp_path):
    """The guard must not break the sources the agreement does not cover."""
    conn = db.init_db(tmp_path / "c.db", force_sqlite=True)
    try:
        for source in ("sleeper", "espn", "fantasypros", "nflverse"):
            db.cache_put(conn, f"{source}:players", source, {"ok": True})
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 4
    finally:
        conn.close()


def test_yahoo_projections_ship_disabled_and_weighted_zero():
    """Obligation 3: optional, default weight zero.

    A weight is one number in a config file and is exactly the kind of thing
    that gets nudged during tuning without anyone remembering it is a contract
    term.
    """
    from src.config import Config

    cfg = Config.load("config.yaml")
    weight = cfg.get("projections.weights.yahoo")
    assert weight in (0, 0.0), f"Yahoo projection weight ships at {weight}, must be 0"
    assert cfg.get("sources.yahoo.enabled") is False, (
        "the Yahoo projection source must ship disabled"
    )


# --- obligation 1: Yahoo responses live in memory for one run ----------------

class _FakeQuery:
    """Stands in for yfpy so these run with no OAuth and no network."""

    def __init__(self, payload=None, fail_after: int | None = None):
        self.payload = payload if payload is not None else {"name": b"Extra Fun League"}
        self.calls = 0
        self.fail_after = fail_after

    def get_league_settings(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("Yahoo is unreachable")
        return self.payload


def _client(tmp_path, query):
    from src.config import Config
    from src.yahoo_client import YahooClient

    cfg = Config.load("config.yaml")
    conn = db.init_db(tmp_path / "y.db", force_sqlite=True)
    client = YahooClient(cfg, conn)
    client._query = query
    client._league_key = "nfl.l.796511"
    return client, conn


def test_a_yahoo_response_never_reaches_disk(tmp_path):
    """The whole obligation, in one assertion."""
    query = _FakeQuery()
    client, conn = _client(tmp_path, query)
    try:
        payload, _ = client._cached("yahoo:settings:x", query.get_league_settings)
        assert payload["name"] == "Extra Fun League"
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0, (
            "a Yahoo response was written to source_cache"
        )
    finally:
        conn.close()


def test_a_response_is_reused_within_the_run(tmp_path):
    """Held in memory for the duration of a run, so one fetch serves the run.

    This is also the cost control: with disk caching forbidden, an unmemoised
    client would re-fetch on every caller, multiplying API calls by however
    many places happen to ask.
    """
    query = _FakeQuery()
    client, conn = _client(tmp_path, query)
    try:
        client._cached("yahoo:settings:x", query.get_league_settings)
        client._cached("yahoo:settings:x", query.get_league_settings)
        client._cached("yahoo:settings:x", query.get_league_settings)
        assert query.calls == 1, f"hit Yahoo {query.calls} times for one key"
    finally:
        conn.close()


def test_the_memory_does_not_outlive_the_run(tmp_path):
    """A second client is a second run, and starts with nothing."""
    query = _FakeQuery()
    client, conn = _client(tmp_path, query)
    try:
        client._cached("yahoo:settings:x", query.get_league_settings)
        second, conn2 = _client(tmp_path, query)
        second._cached("yahoo:settings:x", query.get_league_settings)
        conn2.close()
        assert query.calls == 2, "a run reused another run's Yahoo data"
    finally:
        conn.close()


def test_when_yahoo_is_down_the_job_says_so_instead_of_using_stale_data(tmp_path):
    """The deliberate reversal of this project's own standard 4.

    Everywhere else, an external source that fails falls back to cached data
    with a warning. For Yahoo that fallback is now forbidden - there is no
    stored copy to fall back TO - so a job must fail loudly rather than quietly
    act on last week's roster. A stale roster produces confident, wrong advice,
    which is worse than no advice.
    """
    query = _FakeQuery(fail_after=0)
    client, conn = _client(tmp_path, query)
    try:
        with pytest.raises(ConnectionError):
            client._cached("yahoo:settings:x", query.get_league_settings)
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0
    finally:
        conn.close()


# --- obligation 1: rosters and budgets are a snapshot, not tables ------------

def _players(conn):
    """A handful of our own (non-Yahoo) player records to resolve against."""
    from src.idmap import IdMapper

    idmap = IdMapper(conn)
    return {
        name: idmap.upsert_player(full_name=name, position=pos, team=team)
        for name, pos, team in [
            ("Jahmyr Gibbs", "RB", "DET"),
            ("Puka Nacua", "WR", "LA"),
            ("Ja'Marr Chase", "WR", "CIN"),
        ]
    }


def test_a_yahoo_player_resolves_without_storing_a_yahoo_id(tmp_path):
    """Decision (c): a Yahoo player key is Yahoo data and must not persist.

    Joining a Yahoo roster to our players needs the mapping, so it is rebuilt
    by name each run and held in memory. The cost is a name-match pass and the
    occasional ambiguous name; the benefit is that no Yahoo identifier is ever
    written down.
    """
    from src.yahoo_snapshot import YahooIdIndex

    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    try:
        ours = _players(conn)
        index = YahooIdIndex(conn)

        key = index.resolve({
            "player_key": "449.p.40001", "player_id": "40001",
            "full_name": "Jahmyr Gibbs", "primary_position": "RB",
            "editorial_team_abbr": "DET",
        })
        assert key == ours["Jahmyr Gibbs"]

        # The Yahoo API must not have contributed an identifier. The columns
        # themselves stay: Sleeper publishes Yahoo cross-reference ids in its
        # own free API, and that mapping predates this agreement and carries
        # every non-Yahoo source. What the agreement governs is data obtained
        # FROM YAHOO, so the test is about who wrote the value, not whether a
        # column exists.
        row = conn.fetchone(
            "SELECT yahoo_id, yahoo_key FROM players WHERE player_key=?",
            (ours["Jahmyr Gibbs"],),
        )
        assert row["yahoo_id"] is None and row["yahoo_key"] is None, (
            "resolving a Yahoo player wrote a Yahoo identifier to disk"
        )
    finally:
        conn.close()


def test_the_yahoo_client_cannot_write_identifiers(tmp_path):
    """Structural: the write path is gone, not merely unused.

    _upsert_from_yahoo_player used to pass yahoo_id and yahoo_key straight into
    the players table on every roster sync. It is replaced by resolution, which
    reads and never writes.
    """
    import inspect

    from src import yahoo_client

    source = inspect.getsource(yahoo_client)
    assert "yahoo_id=" not in source, (
        "src/yahoo_client.py still passes a yahoo_id into a write"
    )
    assert "yahoo_key=" not in source, (
        "src/yahoo_client.py still passes a yahoo_key into a write"
    )


def test_a_sleeper_supplied_id_is_used_when_present(tmp_path):
    """Prefer the published cross-reference over guessing at names.

    Sleeper carries `yahoo_id` for most players. Where it exists the join is
    exact, and name matching - which is what breaks on "Ja'Marr" versus
    "JaMarr", or two players sharing a name - is only the fallback.
    """
    from src.yahoo_snapshot import YahooIdIndex

    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    try:
        from src.idmap import IdMapper

        key = IdMapper(conn).upsert_player(
            full_name="Marquise Brown", position="WR", team="KC", yahoo_id="32180",
        )
        index = YahooIdIndex(conn)
        # A name that would NOT match, so only the id can be doing the work.
        assert index.resolve({
            "player_key": "449.p.32180", "player_id": "32180",
            "full_name": "Hollywood Brown", "primary_position": "WR",
        }) == key
    finally:
        conn.close()


def test_an_unmatched_yahoo_player_is_reported_not_invented(tmp_path):
    """A name we cannot match must be visible, not silently dropped.

    Name matching is the weak point of doing this in memory, so the failures
    have to surface. Silently skipping an unmatched player would quietly shrink
    the roster the lineup optimiser sees, and produce confident advice about an
    incomplete team.
    """
    from src.yahoo_snapshot import YahooIdIndex

    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    try:
        _players(conn)
        index = YahooIdIndex(conn)
        assert index.resolve({"full_name": "Nobody At All", "primary_position": "WR"}) is None
        assert index.unmatched == ["Nobody At All"]
    finally:
        conn.close()


def test_a_snapshot_holds_the_roster_and_never_writes_it(tmp_path):
    from src.yahoo_snapshot import LeagueSnapshot, RosterSpot

    conn = db.init_db(tmp_path / "s.db", force_sqlite=True)
    try:
        snap = LeagueSnapshot(league_key="nfl.l.796511", season=2026, week=2)
        snap.rosters.append(RosterSpot("4", "Butt Fumblers", "gibbs|RB", "RB"))
        snap.rosters.append(RosterSpot("4", "Butt Fumblers", "nacua|WR", "WR"))
        snap.rosters.append(RosterSpot("7", "NUB", "chase|WR", "WR"))

        assert snap.roster_keys("4") == ["gibbs|RB", "nacua|WR"]
        assert snap.all_rostered() == {"gibbs|RB", "nacua|WR", "chase|WR"}
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0
    finally:
        conn.close()


# --- obligation 4: attribution ----------------------------------------------

ATTRIBUTION = "Fantasy data provided by Yahoo Fantasy"
YAHOO_URL = "https://fantasy.yahoo.com"


def test_the_dashboard_credits_yahoo_with_a_link():
    """Obligation 4, and the one most likely to be lost to a restyle.

    Asserted against the source rather than a rendered page so it holds however
    the footer is laid out, and fails loudly if someone deletes the line while
    tidying.
    """
    source = pathlib.Path("dashboard.py").read_text(encoding="utf-8")
    assert ATTRIBUTION in source, f"the dashboard no longer says {ATTRIBUTION!r}"
    assert YAHOO_URL in source, "the attribution is not hyperlinked to Yahoo"


# --- obligation 2: read-only -------------------------------------------------

def test_the_client_makes_no_write_requests():
    """The grant is read-only, so no non-GET verb may appear in the client.

    The existing guard checks METHOD NAMES, which catches `add_player` and
    misses `execute("POST", ...)`. Both matter: a write added through a
    generically-named helper would pass the name check.
    """
    import inspect

    from src import yahoo_client

    source = inspect.getsource(yahoo_client).lower()
    for verb in ('"post"', "'post'", '"put"', "'put'", '"delete"', "'delete'",
                 '"patch"', "'patch'"):
        assert verb not in source, f"the client references the {verb} verb"


def test_no_module_calls_a_yfpy_write_helper():
    """yfpy exposes no write methods today, so none may be referenced.

    Named explicitly rather than inferred, so that if yfpy ever grows them this
    fails on the day somebody reaches for one.
    """
    forbidden = ("add_player", "drop_player", "edit_roster", "set_lineup",
                 "post_transaction", "put_roster", "submit_waiver")
    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        offenders += [f"{path}:{f}" for f in forbidden if f in body]
    assert not offenders, f"write calls found: {offenders}"


# --- obligation 1: the sync builds a snapshot, it does not fill tables -------

class _LeagueQuery(_FakeQuery):
    """A whole small league, shaped the way yfpy serialises it."""

    def get_league_teams(self):
        return [
            {"team_id": "4", "name": b"Butt Fumblers", "faab_balance": 50,
             "waiver_priority": 3},
            {"team_id": "7", "name": b"NUB", "faab_balance": 100,
             "waiver_priority": 1},
        ]

    def get_league_transactions(self):
        return [{"transaction_id": 1, "type": "add/drop", "status": "successful",
                 "faab_bid": 12, "players": []}]


def test_the_yahoo_sync_writes_no_rows(tmp_path):
    """The obligation, checked against the real sync path.

    Every one of these tables used to be filled on every sync. If any of them
    gains a row again, this fails - which is the point, because the failure
    mode is silent by nature: nothing breaks when Yahoo data is written, it is
    simply a breach nobody notices.
    """

    query = _LeagueQuery()
    client, conn = _client(tmp_path, query)
    try:
        _players(conn)
        snap = client.collect_snapshot(season=2026, week=2, teams=query.get_league_teams())

        assert snap.budgets["4"].faab_balance == 50
        assert snap.budgets["4"].team_name == "Butt Fumblers"   # decoded, not b'...'
        assert snap.budgets["7"].faab_balance == 100

        for table in ("rosters", "free_agents", "team_budgets", "transactions"):
            count = conn.scalar(f"SELECT COUNT(*) FROM {table}") or 0
            assert count == 0, f"the sync wrote {count} row(s) to {table}"
        assert conn.scalar("SELECT COUNT(*) FROM source_cache") == 0
    finally:
        conn.close()


def test_a_roster_becomes_player_keys_not_rows(tmp_path):
    """The inverted join: Yahoo's side is keys in memory, ours stays in SQL."""

    client, conn = _client(tmp_path, _LeagueQuery())
    try:
        ours = _players(conn)
        snap = client.collect_roster(
            snapshot=client.new_snapshot(2026, 2),
            team_id="4",
            team_name="Butt Fumblers",
            players=[
                {"full_name": "Jahmyr Gibbs", "primary_position": "RB",
                 "selected_position_value": "RB"},
                {"full_name": "Puka Nacua", "primary_position": "WR",
                 "selected_position_value": "WR"},
            ],
        )
        assert snap.roster_keys("4") == [ours["Jahmyr Gibbs"], ours["Puka Nacua"]]
        assert conn.scalar("SELECT COUNT(*) FROM rosters") == 0
    finally:
        conn.close()


# --- obligation 5: purge -----------------------------------------------------

YAHOO_TABLES = ("rosters", "free_agents", "team_budgets", "transactions")


def test_purge_removes_every_yahoo_row_and_keeps_ours(tmp_path):
    """Obligation 5: if the agreement ends, everything Yahoo goes.

    The hard half is what it must NOT delete. Our projections, our blends, our
    draft picks and our players are not Yahoo's, and a purge that took them
    would destroy the application to satisfy a clause about someone else's
    data.
    """
    from src.compliance import purge_yahoo

    conn = db.init_db(tmp_path / "p.db", force_sqlite=True)
    try:
        ours = _players(conn)
        now = db.utcnow()
        conn.execute(
            "INSERT INTO projections(player_key, source, season, week, stats_json, "
            "points, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (ours["Jahmyr Gibbs"], "sleeper", 2026, 0, "{}", 310.0, now),
        )
        conn.execute(
            "INSERT INTO draft_picks(league_key, pick, round, team_key, player_key, "
            "source, recorded_at) VALUES (?,?,?,?,?,?,?)",
            ("nfl.l.796511", 3, 1, "4", ours["Jahmyr Gibbs"], "manual", now),
        )
        conn.execute(
            "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
            "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?)",
            ("nfl.l.796511", "4", "Butt Fumblers", ours["Puka Nacua"], "WR", 2, now),
        )
        conn.execute(
            "INSERT INTO team_budgets(league_key, season, team_key, team_name, "
            "faab_balance, waiver_priority, fetched_at) VALUES (?,?,?,?,?,?,?)",
            ("nfl.l.796511", 2026, "4", "Butt Fumblers", 50, 3, now),
        )
        conn.commit()

        removed = purge_yahoo(conn)

        for table in YAHOO_TABLES:
            assert conn.scalar(f"SELECT COUNT(*) FROM {table}") == 0, table
        assert removed["rosters"] == 1
        assert removed["team_budgets"] == 1

        # ...and everything of ours survives.
        assert conn.scalar("SELECT COUNT(*) FROM players") == 3
        assert conn.scalar("SELECT COUNT(*) FROM projections") == 1
        assert conn.scalar("SELECT COUNT(*) FROM draft_picks") == 1, (
            "the purge deleted draft picks we typed in ourselves"
        )
    finally:
        conn.close()


def test_purge_clears_yahoo_identifiers_contributed_by_yahoo(tmp_path):
    """Yahoo ids sourced from Sleeper stay; the purge is about Yahoo's grant.

    On termination the safe reading is that any Yahoo identifier goes, even one
    Sleeper published - so the purge clears the columns. That costs an exact
    join and falls back to name matching, which is a real cost, and is the
    correct trade when the agreement has ended.
    """
    from src.compliance import purge_yahoo
    from src.idmap import IdMapper

    conn = db.init_db(tmp_path / "p.db", force_sqlite=True)
    try:
        key = IdMapper(conn).upsert_player(
            full_name="Marquise Brown", position="WR", team="KC", yahoo_id="32180",
        )
        purge_yahoo(conn)
        row = conn.fetchone(
            "SELECT yahoo_id, yahoo_key, full_name FROM players WHERE player_key=?",
            (key,),
        )
        assert row["yahoo_id"] is None and row["yahoo_key"] is None
        assert row["full_name"] == "Marquise Brown", "the player himself was deleted"
    finally:
        conn.close()


def test_purge_reports_what_it_did(tmp_path):
    """A compliance action nobody can evidence is not much use."""
    from src.compliance import purge_yahoo

    conn = db.init_db(tmp_path / "p.db", force_sqlite=True)
    try:
        removed = purge_yahoo(conn)
        assert set(removed) >= set(YAHOO_TABLES)
        assert all(isinstance(v, int) for v in removed.values())
    finally:
        conn.close()
