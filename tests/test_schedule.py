"""The schedule the app owns, and noticing a job that did not run.

On Sunday 2026-09-20 the lineup run never fired. GitHub delivered one scheduled
run all day - the injury monitor, three hours late - and the 14:30Z lineup cron
simply never arrived. Kickoff was 17:00Z. No lineup mail went out, and nothing
anywhere noticed: every check this project has is static, and a static check
cannot see an absence.

Two properties follow, and both are tested here:

  1. The schedule is ONE list. The workflow used to repeat every cron in a bash
     `case`, which had already drifted once - the Sunday cron was moved and the
     case still matched the old time (311ad60).
  2. A job that was due and did not run is findable, from job_runs, by asking.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from src import db, schedule


def _utc(day: str, hhmm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{hhmm}:00+00:00")


# --- one schedule, not two ---------------------------------------------------


def test_the_workflow_and_the_schedule_name_the_same_crons():
    """Derived from both sides, so neither can drift (METHOD 3.1)."""
    workflow = Path(".github/workflows/jobs.yml").read_text(encoding="utf-8")
    in_workflow = set(re.findall(r'-\s*cron:\s*"([^"]+)"', workflow))
    assert len(in_workflow) >= 5, f"only {len(in_workflow)} crons found in the workflow"
    assert in_workflow == {entry.cron for entry in schedule.SCHEDULE}


def test_every_scheduled_job_is_a_real_command():
    from src.cli import HANDLERS, build_parser

    known = set(HANDLERS) | {
        action.dest for action in build_parser()._subparsers._group_actions
    }
    parser_commands = set(build_parser()._subparsers._group_actions[0].choices)
    for entry in schedule.SCHEDULE:
        assert entry.job in parser_commands or entry.job in known, entry.job


def test_a_cron_maps_to_its_job():
    assert schedule.job_for_cron("0 12 * * *") == "injuries"
    assert schedule.job_for_cron("30 14 * * 0") == "lineup"


def test_an_unknown_cron_has_no_job():
    assert schedule.job_for_cron("13 13 * * 5") is None


# --- what is due on a given day ---------------------------------------------


def test_a_daily_cron_is_due_every_day():
    for day in ("2026-09-20", "2026-09-21", "2026-09-24"):
        due = {e.job for e in schedule.due_on(_utc(day, "23:59"))}
        assert "injuries" in due, day


def test_a_weekday_cron_is_due_only_on_that_day():
    sunday = {e.job for e in schedule.due_on(_utc("2026-09-20", "23:59"))}
    monday = {e.job for e in schedule.due_on(_utc("2026-09-21", "23:59"))}
    assert "lineup" in sunday and "lineup" not in monday
    assert "recap" in monday and "recap" not in sunday


def test_a_job_is_not_due_before_its_time():
    """Sunday's lineup is timed for 10:30 ET. Running it at 08:00 would land
    inside the dedup window of Thursday's, which is the bug the time exists to
    avoid (see the comment on that cron)."""
    early = {e.job for e in schedule.due_on(_utc("2026-09-20", "13:00"))}
    later = {e.job for e in schedule.due_on(_utc("2026-09-20", "15:00"))}
    assert "lineup" not in early
    assert "lineup" in later


# --- what was due and did not run -------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "sched.db", force_sqlite=True)
    yield connection
    connection.close()


def _record(conn, job, finished: dt.datetime, status="ok"):
    conn.execute(
        "INSERT INTO job_runs(job, status, exit_code, started_at, finished_at) "
        "VALUES (?,?,?,?,?)",
        (job, status, 0, finished.isoformat(), finished.isoformat()),
    )
    conn.commit()


def test_a_due_job_that_never_ran_is_overdue(conn):
    now = _utc("2026-09-20", "16:00")
    assert "lineup" in {e.job for e in schedule.overdue(conn, now)}


def test_a_job_that_ran_after_its_time_is_not_overdue(conn):
    _record(conn, "lineup", _utc("2026-09-20", "14:45"))
    now = _utc("2026-09-20", "16:00")
    assert "lineup" not in {e.job for e in schedule.overdue(conn, now)}


def test_a_run_from_before_the_scheduled_time_does_not_count(conn):
    """Thursday's lineup mail is not Sunday's."""
    _record(conn, "lineup", _utc("2026-09-20", "09:00"))
    now = _utc("2026-09-20", "16:00")
    assert "lineup" in {e.job for e in schedule.overdue(conn, now)}


def test_a_failed_run_still_counts_as_missing(conn):
    _record(conn, "lineup", _utc("2026-09-20", "14:45"), status="failed")
    now = _utc("2026-09-20", "16:00")
    assert "lineup" in {e.job for e in schedule.overdue(conn, now)}


def test_a_job_with_nothing_to_say_counts_as_having_run(conn):
    """"nothing_to_do" is a real answer - the job ran and found no problem."""
    _record(conn, "injuries", _utc("2026-09-20", "12:30"), status="nothing_to_do")
    now = _utc("2026-09-20", "16:00")
    assert "injuries" not in {e.job for e in schedule.overdue(conn, now)}


def test_nothing_is_overdue_before_the_first_job_of_the_day(conn):
    assert schedule.overdue(conn, _utc("2026-09-20", "02:00")) == []


def test_overdue_reports_how_late_it_is(conn):
    entries = schedule.overdue(conn, _utc("2026-09-20", "16:30"))
    lineup = next(e for e in entries if e.job == "lineup")
    assert lineup.due_at == _utc("2026-09-20", "14:30")
