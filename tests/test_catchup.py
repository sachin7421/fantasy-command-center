"""Picking up a job the scheduler dropped, and saying so when it cannot.

Sunday 2026-09-20: the lineup cron never fired, the injury monitor ran three
hours late, and the last recommendation before kickoff never happened. Nothing
reported it, because an absence leaves no trace to check.

So every scheduled run ends by asking what was due and has not run. If it can
run it, it does - a dropped slot is picked up by the next run that day instead
of waiting a week. If it cannot, the failure becomes a notification, because
the whole point is that silence must stop meaning "fine".
"""
from __future__ import annotations

import datetime as dt

import pytest

from src import cli, db


def _utc(day: str, hhmm: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{hhmm}:00+00:00")


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, notification, force=False):
        self.sent.append(notification)
        return {"sent": True}


class _Ctx:
    def __init__(self, conn, notifier):
        self.conn = conn
        self._notifier = notifier

    def notifier(self):
        return self._notifier


@pytest.fixture
def ctx(tmp_path):
    conn = db.init_db(tmp_path / "catchup.db", force_sqlite=True)
    context = _Ctx(conn, _Notifier())
    yield context
    conn.close()


def _ran(ctx, job, finished, status="ok"):
    ctx.conn.execute(
        "INSERT INTO job_runs(job, status, exit_code, started_at, finished_at) "
        "VALUES (?,?,?,?,?)",
        (job, status, 0, finished.isoformat(), finished.isoformat()),
    )
    ctx.conn.commit()


def test_a_dropped_job_is_run(ctx, monkeypatch, capsys):
    """The Sunday that started this: lineup was due at 14:30Z and never ran."""
    run: list[str] = []
    monkeypatch.setattr(cli, "run_scheduled_job", lambda c, job: run.append(job) or 0)

    code = cli.catch_up(ctx, now=_utc("2026-09-20", "16:00"))
    assert code == cli.EXIT_OK
    assert "lineup" in run
    assert "lineup" in capsys.readouterr().out


def test_a_job_that_already_ran_is_left_alone(ctx, monkeypatch):
    run: list[str] = []
    monkeypatch.setattr(cli, "run_scheduled_job", lambda c, job: run.append(job) or 0)
    for job in ("injuries", "lineup"):
        _ran(ctx, job, _utc("2026-09-20", "15:00"))

    cli.catch_up(ctx, now=_utc("2026-09-20", "16:00"))
    assert run == []


def test_nothing_due_yet_says_so_and_sends_nothing(ctx, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_scheduled_job", lambda c, job: 0)
    assert cli.catch_up(ctx, now=_utc("2026-09-20", "02:00")) == cli.EXIT_OK
    assert "nothing overdue" in capsys.readouterr().out.lower()
    assert ctx._notifier.sent == []


def test_a_job_that_cannot_be_recovered_is_reported(ctx, monkeypatch):
    """The watchdog. A job that fails on catch-up is the case where silence
    would otherwise last until someone wondered why no mail arrived."""
    monkeypatch.setattr(cli, "run_scheduled_job", lambda c, job: cli.EXIT_FAIL)

    code = cli.catch_up(ctx, now=_utc("2026-09-20", "16:00"))
    assert code == cli.EXIT_FAIL
    assert len(ctx._notifier.sent) == 1
    text = ctx._notifier.sent[0].text()
    assert "lineup" in text
    assert "14:30" in text, "the notification should say when it was due"


def test_a_successful_catch_up_notifies_nobody(ctx, monkeypatch):
    """The recommendation itself is the mail; this is not a second one."""
    monkeypatch.setattr(cli, "run_scheduled_job", lambda c, job: cli.EXIT_OK)
    cli.catch_up(ctx, now=_utc("2026-09-20", "16:00"))
    assert ctx._notifier.sent == []


def test_the_same_job_is_not_run_twice_in_one_pass(ctx, monkeypatch):
    """Thursday has two entries an hour apart - reminders and lineup - and
    Sunday's list must not repeat a job that appears twice for any reason."""
    run: list[str] = []
    monkeypatch.setattr(cli, "run_scheduled_job", lambda c, job: run.append(job) or 0)
    cli.catch_up(ctx, now=_utc("2026-09-24", "23:00"))  # a Thursday
    assert len(run) == len(set(run)), run


def test_which_job_prints_the_command_for_a_cron(capsys):
    assert cli.main(["which-job", "--cron", "30 14 * * 0"]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "lineup"


def test_which_job_falls_back_to_daily_for_an_unknown_cron(capsys):
    """A cron added to the workflow and not to the schedule must still run
    something sensible rather than nothing."""
    assert cli.main(["which-job", "--cron", "7 7 * * 5"]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "daily"
