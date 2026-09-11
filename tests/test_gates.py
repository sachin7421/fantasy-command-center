"""Tests for the checks themselves.

A gate that has quietly stopped checking is worse than no gate, because it
reports success. `check_degradation` is ~100 lines of AST walking whose whole
job is to fail the build; if a refactor broke it, every run would go green and
nothing would ever say so. So it gets the same treatment as the code it guards:
it must catch what it claims to catch, and it must not cry wolf.
"""
from __future__ import annotations

from tools.check_degradation import check_file


def _write(tmp_path, body: str):
    path = tmp_path / "sample.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_it_catches_a_silent_broad_handler(tmp_path):
    path = _write(tmp_path, "def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    problems = check_file(path)
    assert len(problems) == 1
    assert "swallows the failure" in problems[0]


def test_a_declared_handler_is_accepted(tmp_path):
    path = _write(tmp_path, (
        "def f():\n    try:\n        g()\n"
        "    except Exception:  # silent: g is best-effort telemetry\n        pass\n"
    ))
    assert check_file(path) == []


def test_a_marker_above_the_handler_is_accepted(tmp_path):
    """The reason often needs its own line; both placements must work."""
    path = _write(tmp_path, (
        "def f():\n    try:\n        g()\n"
        "    # silent: g is best-effort telemetry and cannot affect the result\n"
        "    except Exception:\n        pass\n"
    ))
    assert check_file(path) == []


def test_a_logged_handler_is_accepted(tmp_path):
    path = _write(tmp_path, (
        "def f():\n    try:\n        g()\n"
        "    except Exception:\n        log.warning('g failed')\n"
    ))
    assert check_file(path) == []


def test_a_reraising_handler_is_accepted(tmp_path):
    path = _write(tmp_path, "def f():\n    try:\n        g()\n    except Exception:\n        raise\n")
    assert check_file(path) == []


def test_an_error_returned_as_a_value_is_accepted(tmp_path):
    """A health check that hands the failure to its caller has not lost it."""
    path = _write(tmp_path, (
        "def f():\n    try:\n        g()\n"
        "    except Exception as exc:\n        return {'ok': False, 'error': str(exc)}\n"
    ))
    assert check_file(path) == []


def test_binding_the_exception_and_ignoring_it_is_still_caught(tmp_path):
    """`as exc` is not a free pass - the handler has to actually use it."""
    path = _write(tmp_path, (
        "def f():\n    try:\n        g()\n"
        "    except Exception as exc:\n        return None\n"
    ))
    assert len(check_file(path)) == 1


def test_narrow_handlers_are_ignored_by_default_and_caught_under_strict(tmp_path):
    path = _write(tmp_path, (
        "def f():\n    try:\n        return float(x)\n"
        "    except (TypeError, ValueError):\n        return None\n"
    ))
    assert check_file(path) == []
    assert len(check_file(path, strict=True)) == 1


def test_a_bare_except_counts_as_broad(tmp_path):
    path = _write(tmp_path, "def f():\n    try:\n        g()\n    except:\n        pass\n")
    assert len(check_file(path)) == 1


def test_an_unparseable_file_is_a_failure_not_a_pass(tmp_path):
    """The dangerous bug in a linter: skipping what it cannot read.

    A file that does not parse must never be silently counted as clean - that
    is the checker committing the exact sin it exists to find.
    """
    path = _write(tmp_path, "def f(:\n    pass\n")
    problems = check_file(path)
    assert len(problems) == 1
    assert "could not parse" in problems[0]


def test_the_real_source_tree_is_clean():
    """The gate's own current verdict, pinned.

    Without this, the repository could drift back to swallowing failures and
    only a person remembering to run the tool would notice.
    """
    from pathlib import Path

    problems = []
    for path in sorted(Path("src").rglob("*.py")):
        problems.extend(check_file(path))
    problems.extend(check_file(Path("dashboard.py")))
    assert problems == [], "\n".join(problems)


# --- the Yahoo persistence gate ----------------------------------------------

def _write(tmp_path, body: str, name="sample.py"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_it_catches_an_insert_into_a_yahoo_table(tmp_path):
    """The breach this exists for is silent: nothing BREAKS when Yahoo data is
    written, so only a checker notices."""
    from tools.check_yahoo_persistence import check_file

    path = _write(tmp_path, (
        'def sync(conn):\n'
        '    conn.execute("INSERT INTO rosters(league_key) VALUES (?)", (1,))\n'
    ))
    problems = check_file(path)
    assert len(problems) == 1
    assert "rosters" in problems[0]


def test_it_catches_every_yahoo_table(tmp_path):
    from tools.check_yahoo_persistence import check_file

    for table in ("rosters", "free_agents", "team_budgets", "transactions"):
        path = _write(tmp_path, f'x = "INSERT INTO {table} VALUES (1)"\n')
        assert check_file(path), f"{table} not guarded"


def test_it_catches_caching_a_yahoo_payload(tmp_path):
    from tools.check_yahoo_persistence import check_file

    path = _write(tmp_path, 'db.cache_put(conn, key, "yahoo", payload)\n')
    assert check_file(path)


def test_reading_a_yahoo_table_is_not_a_write(tmp_path):
    """The ban is on persisting, not on a migration that drops the tables."""
    from tools.check_yahoo_persistence import check_file

    path = _write(tmp_path, 'x = "SELECT * FROM rosters"\ny = "DROP TABLE rosters"\n')
    assert check_file(path) == []


def test_our_own_tables_are_untouched(tmp_path):
    from tools.check_yahoo_persistence import check_file

    path = _write(tmp_path, 'x = "INSERT INTO projections(player_key) VALUES (1)"\n')
    assert check_file(path) == []


def test_the_real_source_tree_persists_no_yahoo_data():
    """The current verdict, pinned, so the repository cannot drift back."""
    from pathlib import Path

    from tools.check_yahoo_persistence import check_file

    problems = []
    for path in sorted(Path("src").rglob("*.py")):
        problems.extend(check_file(path))
    problems.extend(check_file(Path("dashboard.py")))
    assert problems == [], "\n".join(problems)
