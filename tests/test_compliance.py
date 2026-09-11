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

        # The tables are absent, not merely empty. An empty table is an
        # invitation - the next person who needs a roster finds `rosters`
        # sitting there and fills it - and nothing about an empty one says why
        # it should stay that way.
        for table in ("rosters", "free_agents", "team_budgets", "transactions"):
            assert not conn.table_exists(table), (
                f"{table} still exists; Yahoo league state has somewhere to live"
            )
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
        assert not conn.table_exists("rosters")
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
        # A database from BEFORE the agreement, which is the only kind that has
        # anything to purge. Current schemas do not create these tables at all.
        conn.executescript("""
            CREATE TABLE rosters (league_key TEXT, team_key TEXT, team_name TEXT,
                                  player_key TEXT, selected_pos TEXT, week INTEGER,
                                  fetched_at TEXT);
            CREATE TABLE team_budgets (league_key TEXT, season INTEGER,
                                       team_key TEXT, team_name TEXT,
                                       faab_balance INTEGER, waiver_priority INTEGER,
                                       fetched_at TEXT);
            CREATE TABLE free_agents (league_key TEXT, player_key TEXT,
                                      pct_owned REAL, week INTEGER, fetched_at TEXT);
            CREATE TABLE transactions (league_key TEXT, txn_id TEXT, type TEXT,
                                       timestamp TEXT, payload_json TEXT);
        """)
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


# --- the inverted join -------------------------------------------------------

def test_roster_selection_by_key_needs_no_yahoo_table(tmp_path):
    """The shape every season module is converted to.

    Yahoo's side is a list of keys in memory; ours stays in SQL and is selected
    by them. That removes the cross-source join entirely, which is what made
    the old design need a `rosters` table in the first place.
    """
    from src.yahoo_snapshot import key_clause

    conn = db.init_db(tmp_path / "j.db", force_sqlite=True)
    try:
        ours = _players(conn)
        keys = [ours["Jahmyr Gibbs"], ours["Puka Nacua"]]
        clause, params = key_clause(keys)
        rows = conn.fetchall(
            f"SELECT full_name FROM players WHERE player_key IN ({clause}) "
            "ORDER BY full_name",
            params,
        )
        assert [r["full_name"] for r in rows] == ["Jahmyr Gibbs", "Puka Nacua"]
    finally:
        conn.close()


def test_an_empty_roster_selects_nobody_rather_than_everybody(tmp_path):
    """`IN ()` is a syntax error, and `WHERE 1=1` would return the league.

    The dangerous version of this bug is silent: an empty key list that
    degrades to "match everything" hands the lineup optimiser all 3,297
    players and produces a confident, entirely fictional lineup.
    """
    from src.yahoo_snapshot import key_clause

    conn = db.init_db(tmp_path / "j.db", force_sqlite=True)
    try:
        _players(conn)
        clause, params = key_clause([])
        rows = conn.fetchall(
            f"SELECT full_name FROM players WHERE player_key IN ({clause})", params
        )
        assert rows == []
    finally:
        conn.close()


# --- matchups and standings are Yahoo data too -------------------------------

def test_matchups_and_standings_are_never_stored(tmp_path):
    """They come from the Yahoo API, so the same rule applies.

    Playoff odds needed a schedule and never had a writer for one. The obvious
    fix - sync `matchups` and `standings_history` into tables - would have
    reintroduced exactly what the agreement forbids, in a new pair of tables
    nobody had thought to name. They go on the snapshot instead.
    """

    class _Q(_FakeQuery):
        def get_league_scoreboard_by_week(self, chosen_week):
            return {"matchups": [
                {"week": chosen_week, "teams": [
                    {"team_id": "4", "name": b"Butt Fumblers"},
                    {"team_id": "7", "name": b"NUB"},
                ]},
            ]}

        def get_league_standings(self):
            return {"teams": [
                {"team_id": "4", "name": b"Butt Fumblers",
                 "team_standings": {"rank": 1, "outcome_totals":
                                    {"wins": 8, "losses": 2, "ties": 0},
                                    "points_for": 1200.5}},
            ]}

    query = _Q()
    client, conn = _client(tmp_path, query)
    try:
        snap = client.new_snapshot(2026, 3)
        client.collect_matchups(snap, query.get_league_scoreboard_by_week(3), 3)
        client.collect_standings(snap, query.get_league_standings())

        assert snap.matchups == [(3, "4", "7")]
        assert snap.standings["4"].rank == 1
        assert snap.standings["4"].wins == 8
        assert snap.standings["4"].team_name == "Butt Fumblers"  # decoded

        for table in ("matchups", "standings_history"):
            assert not conn.table_exists(table), f"{table} still exists"
    finally:
        conn.close()


def test_a_matchup_pairs_both_directions_once(tmp_path):
    """Each game is one row, not two.

    Yahoo reports a matchup once with both teams in it. Storing it per team
    double-counted the schedule, which in a Monte Carlo means every team plays
    twice as many games as it really does.
    """

    client, conn = _client(tmp_path, _FakeQuery())
    try:
        snap = client.new_snapshot(2026, 3)
        client.collect_matchups(snap, {"matchups": [
            {"week": 3, "teams": [{"team_id": "4"}, {"team_id": "7"}]},
            {"week": 3, "teams": [{"team_id": "1"}, {"team_id": "2"}]},
        ]}, 3)
        assert len(snap.matchups) == 2
        assert snap.opponent_of("4", 3) == "7"
        assert snap.opponent_of("7", 3) == "4"
        assert snap.opponent_of("9", 3) is None
    finally:
        conn.close()


def test_league_settings_is_a_forbidden_table():
    """The gate missed the table migration 0004 removed for being Yahoo data.

    tools/check_yahoo_persistence listed four tables and not this one, so the
    write in fetch_league_settings passed a gate whose entire stated purpose is
    that this breach is silent. A checker with a hole in it is worse than none,
    because it reports success.
    """
    from tools.check_yahoo_persistence import YAHOO_TABLES

    assert "league_settings" in YAHOO_TABLES


def test_fetching_settings_stores_nothing(tmp_path):
    """Settings fetched from the API are Yahoo's, whatever we do with them.

    The hand transcription in league_bootstrap may be kept - a person read it
    off a screen. What the API returns may not, and this wrote the whole
    payload to disk on every fetch.
    """
    class _Settings(_FakeQuery):
        def get_league_settings(self):
            return {"num_teams": 12, "name": b"Extra Fun League"}

    query = _Settings()
    client, conn = _client(tmp_path, query)
    try:
        payload = client.fetch_league_settings()
        assert payload["num_teams"] == 12
        assert not conn.table_exists("league_settings")
    finally:
        conn.close()


# --- obligation 3, in the code rather than the config ------------------------

def test_a_zero_weighted_source_is_never_blended_even_alone():
    """Weight zero must mean OFF, not "off unless nobody else showed up".

    normalize_weights dropped zero-weighted sources, and when that emptied the
    dict it fell back to equal weighting over whatever was available - so a
    player only Yahoo projects was blended 100% from Yahoo despite a shipped
    weight of 0.0. That is exactly the coverage gap a third source exists to
    fill, which makes it the likeliest case, not a corner one.

    Asserting on the YAML string was not enough; the string was correct all
    along and the arithmetic ignored it.
    """
    from src.projections import normalize_weights

    weights = {"sleeper": 0.5, "espn": 0.5, "yahoo": 0.0}
    assert normalize_weights(["yahoo"], weights) == {}
    assert normalize_weights(["sleeper", "espn", "yahoo"], weights) == {
        "sleeper": 0.5, "espn": 0.5
    }


def test_the_shipped_default_weights_do_not_include_yahoo():
    """DEFAULT_WEIGHTS applies whenever config omits the section."""
    from src.projections import DEFAULT_WEIGHTS

    assert DEFAULT_WEIGHTS.get("yahoo", 0) == 0


def test_an_unconfigured_yahoo_source_gets_no_weight():
    """An unlisted source defaults to a nonzero weight; Yahoo may not."""
    from src.projections import normalize_weights

    assert normalize_weights(["yahoo_daily"], {"sleeper": 1.0}) == {}


# --- purge coverage ----------------------------------------------------------

def test_purge_removes_yahoo_rows_from_the_shared_id_map(tmp_path):
    """1,604 Yahoo player ids were on disk in player_id_map after a purge.

    The purge cleared players.yahoo_id and stopped there. player_id_map is a
    different table with a `source` column, and faab.py reads it with
    WHERE source='yahoo' - so the Yahoo join kept working after a purge that
    reported it had removed every Yahoo identifier.
    """
    from src.compliance import purge_yahoo
    from src.idmap import IdMapper

    conn = db.init_db(tmp_path / "m.db", force_sqlite=True)
    try:
        IdMapper(conn).upsert_player(
            full_name="Marquise Brown", position="WR", team="KC",
            yahoo_id="32180", sleeper_id="4task",
        )
        assert conn.scalar(
            "SELECT COUNT(*) FROM player_id_map WHERE source='yahoo'"
        ) >= 1

        purge_yahoo(conn)

        assert conn.scalar(
            "SELECT COUNT(*) FROM player_id_map WHERE source='yahoo'"
        ) == 0
        # Other sources' mappings are ours and must survive.
        assert conn.scalar(
            "SELECT COUNT(*) FROM player_id_map WHERE source='sleeper'"
        ) >= 1
    finally:
        conn.close()


def test_purge_removes_picks_that_came_from_yahoo_and_keeps_ours(tmp_path):
    """draft_picks holds both. Provenance is in the `source` column."""
    from src.compliance import purge_yahoo

    conn = db.init_db(tmp_path / "d.db", force_sqlite=True)
    try:
        for pick, source in ((3, "manual"), (4, "yahoo"), (5, "skipped")):
            conn.execute(
                "INSERT INTO draft_picks(league_key, pick, round, team_key, "
                "player_key, source, recorded_at) VALUES (?,?,?,?,?,?,?)",
                ("nfl.l.796511", pick, 1, "4", f"p{pick}", source, db.utcnow()),
            )
        conn.commit()
        purge_yahoo(conn)

        left = {r["source"] for r in conn.fetchall("SELECT source FROM draft_picks")}
        assert left == {"manual", "skipped"}, (
            f"purge left {left}; picks synced FROM Yahoo are Yahoo's data, "
            "and picks typed in by hand are ours"
        )
    finally:
        conn.close()


def test_purge_clears_stored_notification_bodies(tmp_path):
    """recommendations.payload_json quotes Yahoo facts verbatim.

    A stored waiver notification names the free-agent pool and the remaining
    FAAB balance; a lineup one names who is started on the Yahoo roster. The
    dashboard re-renders those lines after a purge that claimed to be complete.
    """
    from src.compliance import purge_yahoo

    conn = db.init_db(tmp_path / "r.db", force_sqlite=True)
    try:
        conn.execute(
            "INSERT INTO recommendations(job, season, week, payload_json, "
            "created_at) VALUES (?,?,?,?,?)",
            ("waivers", 2026, 2,
             '{"lines": ["__CLAIMS (FAAB left: $63)__"]}', db.utcnow()),
        )
        conn.commit()
        purge_yahoo(conn)
        assert conn.scalar("SELECT COUNT(*) FROM recommendations") == 0
    finally:
        conn.close()
