"""Monday recap: what happened, and what it cost me (spec 6.5).

The useful part of a recap is not the score, it is the gap between what I scored
and what I *could* have scored - points left on the bench are the one mistake a
manager can actually learn from.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.lineup_solver import best_lineup
from src.notify import Notification
from src.storage import Database


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
    #: Rostered players, and how many of them have a recorded score. A week
    #: that has not been played yet, or whose stats have not synced, scores
    #: everyone at zero - and "0.0 pts, 0.0 left on bench" is a perfectly
    #: plausible-looking recap of a week that never happened.
    roster_size: int = 0
    scored: int = 0

    @property
    def points_left_on_bench(self) -> float:
        return round(self.optimal_points - self.actual_points, 2)

    @property
    def has_data(self) -> bool:
        return self.roster_size > 0 and self.scored > 0


@dataclass
class _Scored:
    player_key: str
    name: str
    position: str
    points: float
    started: bool
    #: Whether a real score was recorded. Distinguishes a genuine zero from a
    #: week that has not been played or synced.
    scored: bool = False


def _actual_week_scores(
    conn: Database, league_key: str, team_key: str, season: int, week: int
) -> list[_Scored]:
    """Actual scored points for my roster that week.

    Uses stored weekly projections as a stand-in when real results have not been
    synced, which keeps the recap useful mid-build; the numbers are labelled.
    """
    # Real scores come from `player_week_actuals`, which `fcc sync-usage` writes.
    #
    # This used to look for `projections` rows with source='actual'. Nothing in
    # the codebase has ever written such a row, so the COALESCE always fell
    # through to the blended PROJECTION and the Monday recap reported what a
    # player was expected to score as what he actually scored - including the
    # "points left on your bench" figure, which was therefore a comparison of
    # two projections.
    rows = conn.execute(
        """
        SELECT r.player_key, r.selected_pos, p.full_name, p.position,
               a.points AS actual_pts,
               b.points AS projected_pts
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN player_week_actuals a
               ON a.player_key=r.player_key AND a.season=? AND a.week=?
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
            # Only a real recorded score counts. A missing one is missing, not
            # zero and not a projection - `has_data` decides whether there is a
            # recap to give at all.
            points=float(r["actual_pts"]) if r["actual_pts"] is not None else 0.0,
            scored=r["actual_pts"] is not None,
            started=bool(
                r["selected_pos"] and r["selected_pos"].upper() not in ("BN", "IR", "IR+", "NA")
            ),
        )
        for r in rows
    ]


def run(
    conn: Database,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
    starting_slots: dict[str, int],
) -> RecapReport:
    roster = _actual_week_scores(conn, league_key, team_key, season, week)
    report = RecapReport(
        week=week,
        roster_size=len(roster),
        scored=sum(1 for p in roster if p.scored),
    )
    if not report.has_data:
        return report

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
    # Deliberately NOT strict: there is no reason the number of players who
    # should have started equals the number who wrongly did, and the shorter
    # list is the number of real mistakes.
    for benched, actual_starter in zip(
        sorted(should_have_started, key=lambda p: p.points, reverse=True),
        wrongly_started, strict=False,
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
    if not report.has_data:
        return Notification(
            title=f"Week {report.week} recap: no scores recorded",
            lines=[
                f"{report.roster_size} rostered player(s), "
                f"{report.scored} with a week {report.week} score.",
                "",
                "Either the week has not been played, or the stat sync has not",
                "run. No recap was produced rather than one full of zeroes.",
            ],
            job="recap",
            season=season,
            week=report.week,
        )
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
