"""The scheduled workflow does what its crons say.

The workflow picks a job by matching `github.event.schedule` against a `case`
of cron strings, which duplicates every cron by hand. When the Sunday lineup
run moved from 09:00 to 10:30 ET the schedule said `30 14 * * 0` and the case
still said `0 13 * * 0`, so from 20 Sep every Sunday would have fallen through
to the default and run `daily` instead of `lineup`. Caught before its first
Sunday: the change landed on the evening of 13 Sep, after that day's run.
"""
from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(".github/workflows/jobs.yml")


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_every_scheduled_cron_selects_a_job_explicitly():
    text = _text()
    scheduled = set(re.findall(r'-\s*cron:\s*"([^"]+)"', text))
    handled = set(re.findall(r'^\s*"([^"]+)"\)\s*echo "job=', text, re.M))
    assert scheduled, "no crons found - the pattern no longer matches the file"
    assert scheduled <= handled, (
        f"crons with no case arm fall through to the default job: "
        f"{sorted(scheduled - handled)}"
    )
    assert handled <= scheduled, (
        f"case arms for crons that never fire: {sorted(handled - scheduled)}"
    )


def test_the_client_id_secret_is_only_visible_to_the_scope_check():
    """As YAHOO_CONSUMER_KEY at job level, it would make every season job treat
    Yahoo as configured and try to authenticate with no token."""
    text = _text()
    uses = [m.start() for m in re.finditer(r"secrets\.YAHOO_CLIENT_ID", text)]
    assert len(uses) == 1
    step = text.rfind("- name:", 0, uses[0])
    step_text = text[step:text.find("- name:", uses[0])]
    assert "fcc.py yahoo-scope" in step_text


def test_the_job_timeout_leaves_room_for_a_full_sync():
    """Runs on 18 and 19 Sep were cancelled mid-sync at 20 minutes.

    `fcc sync` measured 15m20s against the live database on 19 Sep, and the
    budget also covers `doctor` and the job itself. A timeout under half an
    hour cancels the morning run again.
    """
    import re

    match = re.search(r"timeout-minutes:\s*(\d+)", _text())
    assert match, "no job timeout found"
    assert int(match.group(1)) >= 30, (
        f"timeout is {match.group(1)}min; sync alone measured 15m20s and the "
        "20-minute budget cancelled two consecutive morning runs"
    )
