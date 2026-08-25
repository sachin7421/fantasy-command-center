"""Reminder timing and email rendering tests.

Reminders are the one output driven purely by the clock, so the tests pin
*when* each fires rather than what it says.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


from src import email_render
from src.season import reminders

TZ = ZoneInfo("America/New_York")

SETTINGS = {
    "draft_time": "Tue Sep 8 2026 8:30pm",
    "trade_end_date": "2026-11-28",
    "playoff_start_week": 15,
}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def titles(found) -> list[str]:
    return [r.title for r in found]


# --- draft countdown ---------------------------------------------------------

def test_draft_reminder_is_silent_while_it_is_far_away():
    found = reminders.build(SETTINGS, at("2026-08-24T09:00"))
    assert not any("Draft" in t for t in titles(found))


def test_draft_reminder_fires_inside_the_final_week():
    found = reminders.build(SETTINGS, at("2026-09-04T09:00"))
    assert any("Draft" in t for t in titles(found))


def test_draft_reminder_escalates_on_the_day():
    found = reminders.build(SETTINGS, at("2026-09-08T09:00"))
    draft = next(r for r in found if "Draft" in r.title)
    assert draft.urgency == "high"


def test_draft_reminder_stops_once_the_draft_has_passed():
    found = reminders.build(SETTINGS, at("2026-09-10T09:00"))
    assert not any("Draft" in t for t in titles(found))


def test_draft_time_parses_a_unix_timestamp_too():
    """Yahoo returns an epoch through the API and a string on the page."""
    epoch = int(at("2026-09-08T20:30").timestamp())
    found = reminders.build({"draft_time": epoch}, at("2026-09-06T09:00"))
    assert any("Draft" in t for t in titles(found))


def test_unparseable_draft_time_is_ignored_rather_than_crashing():
    """A draft time we cannot read must drop that one reminder, not the job.

    (Other reminders may still fire - 6 Sep is a Sunday - so this asserts the
    absence of the draft reminder specifically.)
    """
    found = reminders.build({"draft_time": "whenever"}, at("2026-09-06T09:00"))
    assert not any("Draft" in r.title for r in found)


# --- weekly rhythm -----------------------------------------------------------

def test_waiver_reminder_fires_tuesday_morning():
    found = reminders.build(SETTINGS, at("2026-09-15T08:00"), week=2)
    assert any("Waivers" in t for t in titles(found))


def test_waiver_reminder_does_not_fire_tuesday_evening():
    """By the evening the claims have already processed; saying so is noise."""
    found = reminders.build(SETTINGS, at("2026-09-15T20:00"), week=2)
    assert not any("Waivers" in t for t in titles(found))


def test_faab_balance_is_included_when_known():
    found = reminders.build(SETTINGS, at("2026-09-15T08:00"), week=2, faab_left=73)
    waivers = next(r for r in found if "Waivers" in r.title)
    assert any("$73" in line for line in waivers.lines)


def test_thursday_lock_reminder():
    found = reminders.build(SETTINGS, at("2026-09-17T10:00"), week=2)
    assert any("Thursday" in t for t in titles(found))


def test_sunday_lineup_reminder():
    found = reminders.build(SETTINGS, at("2026-09-20T09:00"), week=2)
    assert any("lineup" in t.lower() for t in titles(found))


def test_sunday_reminder_escalates_when_the_lineup_cannot_be_filled():
    quiet = reminders.build(SETTINGS, at("2026-09-20T09:00"), week=2)
    urgent = reminders.build(
        SETTINGS, at("2026-09-20T09:00"), week=2,
        roster_gaps=["Week 2: cannot fill RB, TE"],
    )
    assert next(r for r in quiet if "lineup" in r.title.lower()).urgency == "normal"
    gap = next(r for r in urgent if "lineup" in r.title.lower())
    assert gap.urgency == "high"
    assert any("cannot fill" in line for line in gap.lines)


# --- season milestones -------------------------------------------------------

def test_trade_deadline_reminder_fires_inside_ten_days():
    found = reminders.build(SETTINGS, at("2026-11-20T09:00"), week=12)
    assert any("Trade deadline" in t for t in titles(found))


def test_trade_deadline_is_silent_a_month_out():
    found = reminders.build(SETTINGS, at("2026-10-15T09:00"), week=7)
    assert not any("Trade deadline" in t for t in titles(found))


def test_playoff_warning_fires_the_week_before():
    assert any("Playoffs" in t for t in titles(
        reminders.build(SETTINGS, at("2026-12-09T09:00"), week=14)))
    assert not any("Playoffs" in t for t in titles(
        reminders.build(SETTINGS, at("2026-12-02T09:00"), week=13)))


# --- notification assembly ---------------------------------------------------

def test_no_reminders_produces_no_notification():
    assert reminders.to_notification([], 2026) is None


def test_most_urgent_reminder_leads():
    found = [
        reminders.Reminder("Quiet thing", ["a"], urgency="low"),
        reminders.Reminder("Urgent thing", ["b"], urgency="high"),
    ]
    note = reminders.to_notification(found, 2026)
    assert note.title.startswith("Urgent thing")
    assert note.urgency == "high"


def test_multiple_reminders_become_one_message():
    """A morning should produce one email, not three."""
    found = [
        reminders.Reminder("First", ["a"]),
        reminders.Reminder("Second", ["b"]),
        reminders.Reminder("Third", ["c"]),
    ]
    note = reminders.to_notification(found, 2026)
    assert "+2 more" in note.title


# --- email rendering ---------------------------------------------------------

def test_sections_split_on_headings():
    sections = email_render.split_sections(
        ["intro", "__CLAIMS__", "  a", "  b", "__STASHES__", "  c"]
    )
    assert [s.title for s in sections] == [None, "Claims", "Stashes"]


def test_html_escapes_player_names():
    """A stray angle bracket in source data must not break out into markup."""
    html = email_render.render("Title", ["  Player <script>alert(1)</script>"])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_includes_the_dashboard_link_when_configured():
    html = email_render.render("T", ["x"], dashboard_url="https://example.com/app")
    assert "https://example.com/app" in html
    assert "Open the dashboard" in html


def test_html_omits_the_button_without_a_url():
    assert "Open the dashboard" not in email_render.render("T", ["x"])


def test_urgency_changes_the_accent():
    high = email_render.render("T", ["x"], urgency="high")
    normal = email_render.render("T", ["x"], urgency="normal")
    assert high != normal


def test_every_email_states_that_nothing_was_changed():
    """The guardrail must be visible in the message itself, not just the docs."""
    for body in (email_render.render("T", ["x"]), email_render.render_text("T", ["x"])):
        assert "nothing has been changed" in body.lower()


def test_plain_text_alternative_is_readable():
    text = email_render.render_text("Week 2 waivers", ["__CLAIMS__", "  ADD X / DROP Y"])
    assert "CLAIMS" in text
    assert "ADD X" in text
