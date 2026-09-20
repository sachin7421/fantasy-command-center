"""When each job is due, and which ones did not run.

On Sunday 2026-09-20 the lineup run never fired. GitHub delivered one scheduled
run all day - the injury monitor, three hours late - and the 14:30Z lineup cron
never arrived at all. Kickoff was 17:00Z, so the last recommendation before the
games simply did not happen, and nothing noticed: every check this project has
is static, and a static check cannot see an absence.

Two things live here:

  1. **The schedule itself**, once. `.github/workflows/jobs.yml` used to repeat
     every cron in a bash `case`, and it had already drifted - the Sunday cron
     was moved and the case still matched the old time, so Sunday would have run
     the wrong job (311ad60). The workflow now asks `fcc which-job` instead, and
     a test derives both lists and compares them.

  2. **`overdue()`**, which answers "what was due today and has not run". Each
     scheduled run calls it, so a missed slot is picked up by the next run
     rather than waiting a week - and if it still cannot run, `fcc catchup`
     says so out loud.

Times are UTC, because that is what GitHub cron means. The local-time comments
in the workflow are for US Eastern during daylight saving.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    """One cron line: when it fires, what it runs, and why at that time."""

    cron: str
    job: str
    note: str
    #: Set by `due_on`/`overdue` to the moment it was due on the day asked about.
    due_at: dt.datetime | None = None

    @property
    def minute(self) -> int:
        return int(self.cron.split()[0])

    @property
    def hour(self) -> int:
        return int(self.cron.split()[1])

    @property
    def weekday(self) -> int | None:
        """Cron weekday (0 = Sunday), or None for every day."""
        field = self.cron.split()[4]
        return None if field == "*" else int(field)


#: The single source of truth. The workflow reads it through `fcc which-job`.
SCHEDULE: tuple[ScheduledJob, ...] = (
    ScheduledJob("0 11 * * 2", "waivers", "Tue 07:00 ET - before waivers process"),
    ScheduledJob("0 12 * * *", "injuries", "daily 08:00 ET - injury monitor"),
    ScheduledJob("0 14 * * 4", "lineup", "Thu 10:00 ET - before the TNF lock"),
    ScheduledJob(
        "30 14 * * 0", "lineup",
        "Sun 10:30 ET - not 09:00. Thursday 10:00 to Sunday 09:00 is 71 hours "
        "and the dedup window is 72, so an unchanged Thursday recommendation "
        "silently suppressed Sunday's - the last mail before kickoff.",
    ),
    ScheduledJob("0 11 * * 3", "byes", "Wed 07:00 ET - bye / horizon planner"),
    ScheduledJob("0 12 * * 1", "recap", "Mon 08:00 ET - recap + trade scout"),
    ScheduledJob("0 11 * * 4", "reminders", "Thu 07:00 ET - deadline reminders"),
)

#: A run in one of these states means the job happened. "failed" does not, and
#: neither does a job that never appeared.
RAN_STATUSES = ("ok", "nothing_to_do", "skipped")


def job_for_cron(cron: str) -> str | None:
    """The job a firing cron should run, or None if we do not recognise it."""
    for entry in SCHEDULE:
        if entry.cron == cron.strip():
            return entry.job
    return None


def due_on(now: dt.datetime) -> list[ScheduledJob]:
    """Everything due on `now`'s date at or before `now`, each with its due_at.

    "At or before" matters: a job is not due early. Sunday's lineup is timed to
    clear the notification dedup window that swallowed it once, and running it
    three hours sooner would put it back inside.
    """
    # cron: 0 = Sunday. Python: 0 = Monday.
    cron_weekday = (now.weekday() + 1) % 7
    out: list[ScheduledJob] = []
    for entry in SCHEDULE:
        if entry.weekday is not None and entry.weekday != cron_weekday:
            continue
        due_at = now.replace(
            hour=entry.hour, minute=entry.minute, second=0, microsecond=0
        )
        if due_at <= now:
            out.append(
                ScheduledJob(entry.cron, entry.job, entry.note, due_at=due_at)
            )
    return out


def overdue(conn, now: dt.datetime | None = None) -> list[ScheduledJob]:
    """Jobs due today whose time has passed and which have not run since.

    Read from `job_runs`, which every run writes whatever it decided - so this
    asks the record of what happened rather than trusting the scheduler.
    """
    now = now or dt.datetime.now(dt.UTC)
    missing: list[ScheduledJob] = []
    for entry in due_on(now):
        assert entry.due_at is not None
        placeholders = ", ".join("?" for _ in RAN_STATUSES)
        row = conn.fetchone(
            f"SELECT COUNT(*) AS n FROM job_runs WHERE job=? AND finished_at >= ? "
            f"AND status IN ({placeholders})",
            (entry.job, entry.due_at.isoformat(), *RAN_STATUSES),
        )
        count = row["n"] if row is not None else 0
        if not count:
            missing.append(entry)
    return missing
