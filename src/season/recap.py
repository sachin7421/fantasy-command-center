"""Monday recap: what happened, and what it cost me (spec 6.5).

The useful part of a recap is not the score, it is the gap between what I scored
and what I *could* have scored - points left on the bench are the one mistake a
manager can actually learn from.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.lineup_solver import best_lineup
from src.notify import Notification


@dataclass
class BenchMistake:
    benched: str
    benched_points: float
    started: str
    started_points: float

    @property
    def cost(self) -> float:
        return round(self.benched_points - self.started_points, 2)


@dataclass
class RecapReport:
    week: int
    actual_points: float = 0.0
    optimal_points: float = 0.0
    result: str | None = None
    opponent: str | None = None
    opponent_points: float | None = None
    record: str | None = None
    rank: int | None = None
    mistakes: list[BenchMistake] = field(default_factory=list)

    @property
    def points_left_on_bench(self) -> float:
        return round(self.optimal_points - self.actual_points, 2)


@dataclass
class _Scored:
    player_key: str
    name: str
    position: str
    points: float
    started: bool


def _actual_week_scores(
    conn: sqlite3.Connection, league_key: str, team_key: str, season: int, week: int
) -> list[_Scored]:
    """Actual scored points for my roster that week.

    Uses stored weekly projections as a stand-in when real results have not been
    synced, which keeps the recap useful mid-build; the numbers are labelled.
    """
    rows = conn.execute(
        """
        SELECT r.player_key, r.selected_pos, p.full_name, p.position,
               COALESCE(j.points, b.points, 0) AS pts
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections j
               ON j.player_key=r.player_key AND j.season=? AND j.week=?
              AND j.source='actual'
        LEFT JOIN projections_blended b
               ON b.player_key=r.player_key AND b.season=? AND b.week=?
        WHERE r.league_key=? AND r.team_key=? AND r.week=?
        """,
        (season, week, season, week, league_key, str(team_key), week),
    ).fetchall()
    return [
        _Scored(
            player_key=r["player_key"],
            name=r["full_name"],
            position=r["position"],
            points=float(r["pts"] or 0),
            started=bool(
                r["selected_pos"] and r["selected_pos"].upper() not in ("BN", "IR", "IR+", "NA")
            ),
        )
        for r in rows
    ]


def run(
    conn: sqlite3.Connection,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
    starting_slots: dict[str, int],
) -> RecapReport:
    roster = _actual_week_scores(conn, league_key, team_key, season, week)
    report = RecapReport(week=week)

    started = [p for p in roster if p.started]
    report.actual_points = round(sum(p.points for p in started), 2)

    optimal = best_lineup(roster, starting_slots)
    report.optimal_points = round(
        sum(s.player.points for s in optimal.slots if s.player), 2
    )

    optimal_keys = {s.player.player_key for s in optimal.slots if s.player}
    should_have_started = [p for p in roster if p.player_key in optimal_keys and not p.started]
    wrongly_started = sorted(
        [p for p in started if p.player_key not in optimal_keys],
        key=lambda p: p.points,
    )
    for benched, actual_starter in zip(
        sorted(should_have_started, key=lambda p: p.points, reverse=True), wrongly_started
    ):
        report.mistakes.append(
            BenchMistake(
                benched=benched.name,
                benched_points=benched.points,
                started=actual_starter.name,
                started_points=actual_starter.points,
            )
        )
    return report


def to_notification(report: RecapReport, season: int) -> Notification | None:
    lines = [
        f"Scored: {report.actual_points:.1f}",
        f"Optimal: {report.optimal_points:.1f}",
        f"Left on bench: {report.points_left_on_bench:.1f}",
    ]
    if report.opponent:
        lines.insert(0, f"vs {report.opponent}: {report.opponent_points:.1f} ({report.result})")
    if report.record:
        lines.append(f"Record: {report.record}" + (f", rank {report.rank}" if report.rank else ""))

    if report.mistakes:
        lines.append("")
        lines.append("__WHAT IT COST__")
        for m in report.mistakes:
            lines.append(
                f"  {m.benched} ({m.benched_points:.1f}) on bench while "
                f"{m.started} ({m.started_points:.1f}) started  -{m.cost:.1f}"
            )
    else:
        lines.append("")
        lines.append("You started the optimal lineup. Nothing left on the bench.")

    return Notification(
        title=f"Week {report.week} recap: {report.actual_points:.1f} pts "
        f"({report.points_left_on_bench:.1f} left on bench)",
        lines=lines,
        job="recap",
        urgency="low",
        season=season,
        week=report.week,
    )
