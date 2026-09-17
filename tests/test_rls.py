"""Row-level security is enforced for every table, not the remembered ones.

Supabase flagged nine tables as world-readable and world-WRITABLE over
PostgREST: `my_roster`, `job_runs`, `depth_charts`, `game_context`,
`practice_reports`, `player_week_usage`, `source_accuracy`,
`recommendation_outcomes` and `schema_version`.

The interesting part is how it happened. Thirteen tables had been secured by
hand at some point. Nine more were added over the following months, and not one
inherited it, because nothing made it automatic. A migration would have closed
those nine and left the tenth to repeat the whole story.

So the property under test is not "these tables are secured" - it is "a table
nobody thought about is secured", which is the only version that stays true.
"""
from __future__ import annotations

from src import schema


class _FakePostgres:
    """Enough Database surface for enforce_rls, recording what it was asked."""

    is_postgres = True
    dialect = "postgres"

    def __init__(self, unsecured):
        self._unsecured = list(unsecured)
        self.statements: list[str] = []
        self.commits = 0

    def fetchall(self, sql, params=None):
        assert "NOT rowsecurity" in sql, "must ask for the UNSECURED tables"
        return [(name,) for name in self._unsecured]

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def commit(self):
        self.commits += 1


def test_every_unsecured_table_is_secured():
    conn = _FakePostgres(["my_roster", "job_runs", "a_table_added_next_year"])
    secured = schema.enforce_rls(conn)

    assert secured == ["my_roster", "job_runs", "a_table_added_next_year"]
    for table in secured:
        assert any(
            f'"{table}"' in s and "ENABLE ROW LEVEL SECURITY" in s
            for s in conn.statements
        ), f"{table} was never secured"


def test_a_table_nobody_listed_is_still_secured():
    """The actual regression: enforcement must not depend on a hardcoded list.

    If someone replaces the loop with a fixed set of table names, this fails.
    """
    conn = _FakePostgres(["table_invented_for_this_test"])
    assert schema.enforce_rls(conn) == ["table_invented_for_this_test"]


def test_no_policy_is_ever_created():
    """`RLS on, no policy` denies anon everything. A policy would reopen it.

    Our own connection is the `postgres` role, which bypasses RLS, so we never
    need a policy to keep working - and any CREATE POLICY here would be granting
    access to exactly the anonymous caller the fix exists to shut out.
    """
    conn = _FakePostgres(["players", "my_roster"])
    schema.enforce_rls(conn)
    assert not any("POLICY" in s.upper() for s in conn.statements)


def test_nothing_to_do_costs_one_query_and_no_commit():
    conn = _FakePostgres([])
    assert schema.enforce_rls(conn) == []
    assert conn.statements == []
    assert conn.commits == 0


def test_sqlite_is_untouched():
    """SQLite has no RLS and no PostgREST in front of it."""

    class _FakeSqlite:
        is_postgres = False
        dialect = "sqlite"

        def fetchall(self, sql, params=None):
            raise AssertionError("must not query a SQLite database about RLS")

    assert schema.enforce_rls(_FakeSqlite()) == []


def test_a_failing_check_warns_rather_than_taking_the_app_down(caplog):
    """Standard 4: no silent failures. Also: do not fail closed into an outage.

    A database that refuses the catalog query is one where we cannot tell
    whether tables are exposed. Refusing to start would turn a check into an
    outage; saying nothing is how the hole stayed open for months.
    """

    class _Broken(_FakePostgres):
        def fetchall(self, sql, params=None):
            raise RuntimeError("catalog unavailable")

    with caplog.at_level("WARNING"):
        assert schema.enforce_rls(_Broken([])) == []
    assert any("row-level security" in r.message.lower() for r in caplog.records)


def test_one_table_failing_does_not_abandon_the_rest():
    """A permission error on one table must not leave the others exposed."""

    class _PartlyBroken(_FakePostgres):
        def execute(self, sql, params=None):
            if "locked_down" in sql:
                raise RuntimeError("permission denied")
            return super().execute(sql, params)

    conn = _PartlyBroken(["locked_down", "my_roster"])
    assert schema.enforce_rls(conn) == ["my_roster"]
