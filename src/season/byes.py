"""Bye & horizon planner, plus playoff strength of schedule (spec 6.4).

Two questions:
  1. In the next four weeks, is there a week where I cannot field a legal
     lineup? If so, which available add fixes it?
  2. Do my starters face brutal matchups in the playoff weeks (15-17), and are
     there free agents with elite playoff schedules worth stashing now?
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.lineup_solver import best_lineup
from src.notify import Notification
from src.storage import Database


@dataclass
class WeekOutlook:
    week: int
    available: int
    on_bye: list[str] = field(default_factory=list)
    injured_out: list[str] = field(default_factory=list)
    can_fill_lineup: bool = True
    empty_slots: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


@dataclass
class PlayoffOutlook:
    player: str
    position: str
    team: str
    opponents: dict[int, str] = field(default_factory=dict)
    difficulty: float | None = None


@dataclass
class ByeReport:
    weeks: list[WeekOutlook] = field(default_factory=list)
    playoff: list[PlayoffOutlook] = field(default_factory=list)
    playoff_available: bool = False
    current_week: int = 0
    #: Size of the roster the outlook was computed from. An empty roster cannot
    #: fill a lineup in any week, so every week came back a "problem" and the
    #: planner raised a four-week alarm about having no data.
    roster_size: int = 0

    @property
    def problem_weeks(self) -> list[WeekOutlook]:
        return [w for w in self.weeks if not w.can_fill_lineup]

    @property
    def has_data(self) -> bool:
        return self.roster_size > 0


@dataclass
class _P:
    """Minimal player shape for the lineup solver."""

    player_key: str
    name: str
    position: str
    points: float


def _roster_rows(
    conn: Database, league_key: str, team_key: str, season: int, week: int
):
    return conn.execute(
        """
        SELECT r.player_key, p.full_name, p.position, p.team, p.bye_week,
               COALESCE(b.points, j.points, 0) AS pts,
               i.status AS injury_status
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key=r.player_key AND b.season=:season AND b.week=0
        LEFT JOIN projections j
               ON j.player_key=r.player_key AND j.season=:season AND j.week=0
              AND j.source='sleeper'
        LEFT JOIN (
            SELECT player_key, status,
                   ROW_NUMBER() OVER (PARTITION BY player_key ORDER BY observed_at DESC) rn
            FROM injuries
        ) i ON i.player_key=r.player_key AND i.rn=1
        WHERE r.league_key=:league AND r.team_key=:team AND r.week=:week
        """,
        {"season": season, "league": league_key, "team": str(team_key), "week": week},
    ).fetchall()


def run(
    conn: Database,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
    starting_slots: dict[str, int],
    horizon: int = 4,
    playoff_weeks: tuple[int, ...] = (15, 16, 17),
) -> ByeReport:
    rows = _roster_rows(conn, league_key, team_key, season, week)
    report = ByeReport(current_week=week, roster_size=len(rows))
    if not report.has_data:
        return report

    # The horizon starts at NEXT week. "the next four weeks are covered"
    # previously included the current one, so it reported on three.
    for offset in range(1, horizon + 1):
        target = week + offset
        on_bye, injured, roster = [], [], []
        for r in rows:
            bye = r["bye_week"]
            status = (r["injury_status"] or "")
            if bye and int(bye) == target:
                on_bye.append(r["full_name"])
                continue
            if status in ("IR", "PUP", "Out", "Suspended"):
                injured.append(f"{r['full_name']} ({status})")
                continue
            roster.append(
                _P(r["player_key"], r["full_name"], r["position"], float(r["pts"] or 0))
            )

        lineup = best_lineup(roster, starting_slots)
        outlook = WeekOutlook(
            week=target,
            available=len(roster),
            on_bye=on_bye,
            injured_out=injured,
            can_fill_lineup=lineup.is_complete,
            empty_slots=lineup.empty_slots,
        )
        if not lineup.is_complete:
            for slot in set(lineup.empty_slots):
                fix = _best_available_for_slot(conn, league_key, season, slot, target)
                if fix:
                    outlook.suggestions.append(f"{slot}: add {fix}")
        report.weeks.append(outlook)

    report.playoff, report.playoff_available = _playoff_outlook(
        conn, rows, season, playoff_weeks
    )
    return report


def _best_available_for_slot(
    conn: Database, league_key: str, season: int, slot: str, week: int
) -> str | None:
    """The best free agent who can fill an empty slot that week."""
    from src.lineup_solver import slot_accepts

    rows = conn.execute(
        """
        SELECT p.full_name, p.position, p.bye_week,
               COALESCE(b.points, j.points, 0) AS pts
        FROM free_agents f
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key=f.player_key AND b.season=? AND b.week=0
        LEFT JOIN projections j
               ON j.player_key=f.player_key AND j.season=? AND j.week=0
              AND j.source='sleeper'
        WHERE f.league_key=? AND f.week=(
                  SELECT MAX(week) FROM free_agents WHERE league_key=?
              )
        ORDER BY pts DESC LIMIT 100
        """,
        (season, season, league_key, league_key),
    ).fetchall()
    for r in rows:
        if r["bye_week"] and int(r["bye_week"]) == week:
            continue
        if slot_accepts(slot, r["position"]):
            return f"{r['full_name']} ({r['position']}, {r['pts']:.0f} proj)"
    return None


def _playoff_outlook(
    conn: Database, rows, season: int, playoff_weeks: tuple[int, ...]
) -> tuple[list[PlayoffOutlook], bool]:
    """Playoff-week opponents per starter, when schedule data is available.

    Degrades to an empty result rather than failing the job when nflverse
    schedule data has not been synced.
    """
    schedule = _load_schedule(conn, season, playoff_weeks)
    if not schedule:
        return [], False

    out = []
    for r in rows:
        team = r["team"]
        if not team:
            continue
        opponents = {
            wk: opp for (wk, tm), opp in schedule.items() if tm == team and wk in playoff_weeks
        }
        if opponents:
            out.append(
                PlayoffOutlook(
                    player=r["full_name"], position=r["position"], team=team,
                    opponents=opponents,
                )
            )
    return out, True


def _load_schedule(
    conn: Database, season: int, weeks: tuple[int, ...]
) -> dict[tuple[int, str], str]:
    """(week, team) -> opponent, from the cached nflverse schedule if present."""
    cached = None
    try:
        from src import db

        cached = db.cache_get(conn, f"nflverse:schedule:{season}")
    except Exception:
        return {}
    if not cached:
        return {}
    payload, _ = cached
    out: dict[tuple[int, str], str] = {}
    for game in payload or []:
        wk = game.get("week")
        if wk not in weeks:
            continue
        home, away = game.get("home_team"), game.get("away_team")
        if home and away:
            out[(wk, home)] = away
            out[(wk, away)] = home
    return out


def to_notification(report: ByeReport, season: int) -> Notification | None:
    if not report.has_data:
        return Notification(
            title="Bye planner: no roster to plan around",
            lines=[
                "No players are stored for your team, so every week looks like",
                "a gap. Run `fcc sync-league` once Yahoo access is approved.",
            ],
            job="byes",
            season=season,
            week=report.current_week,
        )

    lines: list[str] = []

    for outlook in report.weeks:
        flags = []
        if outlook.on_bye:
            flags.append(f"bye: {', '.join(outlook.on_bye)}")
        if outlook.injured_out:
            flags.append(f"out: {', '.join(outlook.injured_out)}")
        marker = "OK " if outlook.can_fill_lineup else "GAP"
        lines.append(f"  [{marker}] Week {outlook.week}: {outlook.available} available")
        for flag in flags:
            lines.append(f"        {flag}")
        if not outlook.can_fill_lineup:
            lines.append(f"        cannot fill: {', '.join(outlook.empty_slots)}")
            for suggestion in outlook.suggestions:
                lines.append(f"        fix -> {suggestion}")

    if not report.problem_weeks:
        lines.append("")
        lines.append("No lineup gaps in the next 4 weeks.")

    if report.playoff:
        lines.append("")
        lines.append("__PLAYOFF WEEKS (15-17)__")
        for outlook in report.playoff[:10]:
            matchups = ", ".join(f"W{w} vs {o}" for w, o in sorted(outlook.opponents.items()))
            lines.append(f"  {outlook.player} ({outlook.team}): {matchups}")
    elif not report.playoff_available:
        lines.append("")
        lines.append("Playoff SOS unavailable: run `fcc sync-schedule` to load it.")

    urgency = "high" if report.problem_weeks else "low"
    return Notification(
        title=(
            f"Bye planner: {len(report.problem_weeks)} problem week(s)"
            if report.problem_weeks
            else "Bye planner: next 4 weeks are covered"
        ),
        lines=lines,
        job="byes",
        urgency=urgency,
        season=season,
        week=report.current_week,
    )
