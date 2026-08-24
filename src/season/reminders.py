"""Time-based reminders (draft, lineup locks, waivers, deadlines).

The other jobs answer "what should I do about my roster". This one answers
"is something about to close". Those are different failure modes: a bad lineup
costs you points, but a missed deadline costs you the whole option.

Every reminder is derived from dates the system already knows - the league's own
draft time, trade deadline and playoff weeks - rather than a hand-kept calendar,
so they cannot drift out of sync with the league.

Deliberately quiet: a reminder only fires inside its window, and the notifier
deduplicates on content, so a daily run does not produce a daily nag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.notify import Notification


@dataclass
class Reminder:
    title: str
    lines: list[str] = field(default_factory=list)
    urgency: str = "normal"
    subtitle: str = ""


def _parse_draft_time(raw: Any, tz: ZoneInfo) -> datetime | None:
    """Parse the league draft time.

    Yahoo gives this either as a unix timestamp or as the human string shown on
    the settings page ("Tue Sep 8 8:30pm EDT"), so both are handled.
    """
    if raw in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz)
    except (TypeError, ValueError, OSError):
        pass

    text = str(raw).replace("EDT", "").replace("EST", "").strip()
    # Yahoo omits the year ("Tue Sep 8 8:30pm"). Parsing a year-less date is
    # ambiguous and Python 3.15 will refuse it, so supply the current year up
    # front rather than patching it afterwards.
    candidates = [text]
    if not re.search(r"\d{4}", text):
        parts = text.split()
        if len(parts) >= 3:
            candidates.insert(0, " ".join(parts[:3] + [str(date.today().year)] + parts[3:]))

    for candidate in candidates:
        for fmt in ("%a %b %d %Y %I:%M%p", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=tz)
            except ValueError:
                continue
    return None


def _describe_delta(delta: timedelta) -> str:
    days = delta.days
    hours = delta.seconds // 3600
    if days > 1:
        return f"in {days} days"
    if days == 1:
        return "tomorrow"
    if hours > 1:
        return f"in {hours} hours"
    if hours == 1:
        return "in about an hour"
    return "shortly"


def build(
    settings: dict[str, Any],
    now: datetime,
    *,
    week: int | None = None,
    roster_gaps: list[str] | None = None,
    faab_left: int | None = None,
) -> list[Reminder]:
    """Every reminder that applies right now."""
    tz = now.tzinfo or ZoneInfo("America/New_York")
    out: list[Reminder] = []

    # -- draft -------------------------------------------------------------
    draft_at = _parse_draft_time(settings.get("draft_time"), tz)
    if draft_at and draft_at > now:
        until = draft_at - now
        if until <= timedelta(days=7):
            urgency = "high" if until <= timedelta(hours=24) else "normal"
            out.append(
                Reminder(
                    title=f"Draft {_describe_delta(until)}",
                    subtitle=draft_at.strftime("%A %d %B, %I:%M %p").replace(" 0", " "),
                    urgency=urgency,
                    lines=[
                        f"  Draft starts {draft_at.strftime('%a %d %b at %I:%M %p')}.",
                        "",
                        "__BEFORE IT STARTS__",
                        "  Run `python fcc.py sync` so projections and injuries are current.",
                        "  Open the dashboard and confirm your draft slot in the sidebar.",
                        "  Rehearse with `python fcc.py mockdraft --slot <your slot>`.",
                    ],
                )
            )

    # -- weekly rhythm ------------------------------------------------------
    weekday = now.weekday()          # Monday = 0
    hour = now.hour

    # Waivers process at Tuesday game time in this league, so Monday evening and
    # Tuesday morning are the windows that matter.
    if weekday == 1 and hour < 12:
        lines = ["  Waiver claims process today. Submit before kickoff."]
        if faab_left is not None:
            lines.append(f"  FAAB remaining: ${faab_left}.")
        lines.append("  Run `python fcc.py waivers` for ranked ADD/DROP pairs and bids.")
        out.append(
            Reminder(
                title="Waivers process today",
                subtitle="Claims must be in before the first kickoff",
                urgency="high",
                lines=lines,
            )
        )

    # Thursday night football locks the players on those two teams.
    if weekday == 3 and 8 <= hour < 20:
        out.append(
            Reminder(
                title="Thursday night lock",
                subtitle="Anyone playing tonight locks at kickoff",
                urgency="normal",
                lines=[
                    "  Players on tonight's two teams lock when the game starts.",
                    "  If one of them is questionable, decide now rather than Sunday.",
                    "  Run `python fcc.py lineup` for the optimal lineup.",
                ],
            )
        )

    # Sunday morning is the last realistic chance to fix a lineup.
    if weekday == 6 and 7 <= hour < 13:
        lines = ["  Most games kick off at 1pm. Last chance to set your lineup."]
        if roster_gaps:
            lines.append("")
            lines.append("__PROBLEMS__")
            lines.extend(f"  {gap}" for gap in roster_gaps)
        out.append(
            Reminder(
                title="Set your lineup",
                subtitle="Kickoff is a few hours away",
                urgency="high" if roster_gaps else "normal",
                lines=lines,
            )
        )

    # -- season milestones ---------------------------------------------------
    trade_end = settings.get("trade_end_date")
    if trade_end:
        try:
            deadline = datetime.combine(
                date.fromisoformat(str(trade_end)), time(23, 59), tzinfo=tz
            )
            until = deadline - now
            if timedelta(0) < until <= timedelta(days=10):
                out.append(
                    Reminder(
                        title=f"Trade deadline {_describe_delta(until)}",
                        subtitle=deadline.strftime("%A %d %B"),
                        urgency="normal",
                        lines=[
                            f"  Trades close {deadline.strftime('%a %d %b')}.",
                            "  Run `python fcc.py trades` for mutually beneficial ideas.",
                        ],
                    )
                )
        except ValueError:
            pass

    playoff_start = settings.get("playoff_start_week")
    if week and playoff_start and int(playoff_start) - int(week) == 1:
        out.append(
            Reminder(
                title="Playoffs start next week",
                subtitle=f"Week {playoff_start}",
                urgency="normal",
                lines=[
                    "  This is the last week to stash for the playoff run.",
                    "  Run `python fcc.py byes` for playoff-week schedules.",
                    "  Spend leftover FAAB now - it is worth nothing afterwards.",
                ],
            )
        )

    return out


def to_notification(
    reminders: list[Reminder], season: int, week: int | None = None
) -> Notification | None:
    if not reminders:
        return None

    # Lead with the most urgent thing; the rest follow in one message rather
    # than several, so a single morning produces a single email.
    order = {"high": 0, "normal": 1, "low": 2}
    reminders = sorted(reminders, key=lambda r: order.get(r.urgency, 1))
    primary = reminders[0]

    lines: list[str] = []
    for i, reminder in enumerate(reminders):
        if i:
            lines.append("")
            lines.append(f"__{reminder.title}__")
        lines.extend(reminder.lines)

    title = (
        primary.title if len(reminders) == 1
        else f"{primary.title} (+{len(reminders) - 1} more)"
    )
    return Notification(
        title=title,
        lines=lines,
        job="reminders",
        urgency=primary.urgency,
        season=season,
        week=week,
        subtitle=primary.subtitle,
    )
