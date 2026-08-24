"""Weekly lineup optimizer (spec 6.3).

Solves the best legal lineup from my roster, compares it to what is actually set
in Yahoo, and alerts ONLY on differences - with the reasoning attached, so a
suggested swap can be judged rather than obeyed.

Risk mode (spec 6.3): projected underdogs tilt toward ceiling, favourites toward
floor, using the uncertainty band from src/projections.py.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.lineup_solver import Lineup, best_lineup
from src.notify import Notification
from src.projections import resolve_risk_mode, risk_adjusted_points
from src.sources.sleeper import severity_of

#: Statuses that make a player unstartable regardless of projection.
UNSTARTABLE = {"Out", "IR", "PUP", "Suspended", "NA", "DNR"}


@dataclass
class RosterPlayer:
    player_key: str
    name: str
    position: str
    team: str
    selected_pos: str | None
    points: float
    floor: float
    ceiling: float
    injury_status: str | None
    bye_week: int | None
    on_bye: bool = False

    @property
    def startable(self) -> bool:
        if self.on_bye:
            return False
        return (self.injury_status or "Healthy") not in UNSTARTABLE

    def effective(self, mode: str) -> float:
        if not self.startable:
            return -1.0
        return risk_adjusted_points(self.points, self.floor, self.ceiling, mode)


@dataclass
class Swap:
    slot: str
    bench_in: RosterPlayer
    starter_out: RosterPlayer | None
    gain: float
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        out = self.starter_out.name if self.starter_out else "(empty)"
        line = (
            f"{self.slot}: START {self.bench_in.name} "
            f"({self.bench_in.points:.1f}) over {out}"
        )
        if self.starter_out:
            line += f" ({self.starter_out.points:.1f})"
        line += f"  +{self.gain:.1f}"
        if self.reasons:
            line += "\n    " + "; ".join(self.reasons)
        return line


@dataclass
class LineupReport:
    optimal: Lineup
    current_points: float
    optimal_points: float
    swaps: list[Swap] = field(default_factory=list)
    risk_mode: str = "neutral"
    week: int = 0
    warnings: list[str] = field(default_factory=list)
    #: Rostered players, and how many of them have a projection for this week.
    #: Without this the report cannot tell "your lineup is already optimal"
    #: apart from "every projection is missing, so everything scores zero" -
    #: and it was reporting the second as the first, by email, every week.
    roster_size: int = 0
    projected: int = 0

    @property
    def gain(self) -> float:
        return round(self.optimal_points - self.current_points, 2)

    @property
    def has_data(self) -> bool:
        """Whether anything here is worth believing."""
        return self.roster_size > 0 and self.projected > 0


def load_roster(
    conn: sqlite3.Connection,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
) -> list[RosterPlayer]:
    rows = conn.execute(
        """
        SELECT r.player_key, r.selected_pos, p.full_name, p.position, p.team, p.bye_week,
               COALESCE(b.points, j.points, 0)  AS points,
               COALESCE(b.floor,  j.points, 0)  AS floor,
               COALESCE(b.ceiling,j.points, 0)  AS ceiling,
               i.status                         AS injury_status
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key=r.player_key AND b.season=:season AND b.week=:week
        LEFT JOIN projections j
               ON j.player_key=r.player_key AND j.season=:season AND j.week=:week
              AND j.source='sleeper'
        LEFT JOIN (
            SELECT player_key, status,
                   ROW_NUMBER() OVER (PARTITION BY player_key ORDER BY observed_at DESC) rn
            FROM injuries
        ) i ON i.player_key=r.player_key AND i.rn=1
        WHERE r.league_key=:league AND r.team_key=:team AND r.week=:roster_week
        """,
        {
            "season": season, "week": week, "league": league_key,
            "team": str(team_key), "roster_week": week,
        },
    ).fetchall()

    out = []
    for r in rows:
        out.append(
            RosterPlayer(
                player_key=r["player_key"],
                name=r["full_name"],
                position=r["position"],
                team=r["team"] or "",
                selected_pos=r["selected_pos"],
                points=float(r["points"] or 0),
                floor=float(r["floor"] or 0),
                ceiling=float(r["ceiling"] or 0),
                injury_status=r["injury_status"],
                bye_week=r["bye_week"],
                on_bye=bool(r["bye_week"] and int(r["bye_week"]) == week),
            )
        )
    return out


def run(
    conn: sqlite3.Connection,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
    starting_slots: dict[str, int],
    risk_mode: str = "auto",
    projected_margin: float | None = None,
    min_gap: float = 1.5,
) -> LineupReport:
    roster = load_roster(conn, league_key, team_key, season, week)
    mode = resolve_risk_mode(risk_mode, projected_margin)
    projected = sum(1 for p in roster if p.points > 0)

    optimal = best_lineup(
        roster,
        starting_slots,
        points_of=lambda p: p.effective(mode),
        position_of=lambda p: p.position,
    )

    currently_starting = {
        p.player_key for p in roster
        if p.selected_pos and p.selected_pos.upper() not in ("BN", "IR", "IR+", "NA")
    }
    current_points = sum(
        p.points for p in roster if p.player_key in currently_starting
    )
    optimal_points = sum(
        s.player.points for s in optimal.slots if s.player is not None
    )

    swaps: list[Swap] = []
    for slot in optimal.slots:
        player: RosterPlayer | None = slot.player
        if player is None or player.player_key in currently_starting:
            continue
        # This player should start but currently does not. Find who he displaces.
        displaced = [
            p for p in roster
            if p.player_key in currently_starting
            and p.player_key not in {s.player.player_key for s in optimal.slots if s.player}
        ]
        out_player = min(displaced, key=lambda p: p.points, default=None)
        gain = player.points - (out_player.points if out_player else 0.0)

        reasons = []
        if out_player and out_player.on_bye:
            reasons.append(f"{out_player.name} is on bye")
        if out_player and not out_player.startable and not out_player.on_bye:
            reasons.append(f"{out_player.name} is {out_player.injury_status}")
        if gain > 0 and not reasons:
            reasons.append(f"projection gap {gain:+.1f} pts")
        if mode != "neutral":
            reasons.append(f"{mode} tilt applied (you are the {'underdog' if mode=='ceiling' else 'favourite'})")

        if gain >= min_gap or (out_player and not out_player.startable):
            swaps.append(Swap(slot.slot, player, out_player, round(gain, 2), reasons))
        if out_player:
            currently_starting.discard(out_player.player_key)
        currently_starting.add(player.player_key)

    warnings = []
    if not optimal.is_complete:
        warnings.append(
            f"Cannot fill {', '.join(optimal.empty_slots)} - roster is short this week."
        )
    for p in roster:
        if p.on_bye and p.selected_pos and p.selected_pos.upper() not in ("BN", "IR"):
            warnings.append(f"{p.name} is starting but on bye week {p.bye_week}.")

    return LineupReport(
        optimal=optimal,
        current_points=round(current_points, 2),
        optimal_points=round(optimal_points, 2),
        swaps=swaps,
        risk_mode=mode,
        week=week,
        warnings=warnings,
        roster_size=len(roster),
        projected=projected,
    )


def to_notification(report: LineupReport, season: int) -> Notification | None:
    # Silence beats a confident wrong answer. With no roster or no projections
    # every player scores zero, the optimal lineup equals the current one, and
    # the notification read "0 change(s) suggested" - indistinguishable from a
    # lineup that is genuinely already right.
    if not report.has_data:
        return Notification(
            title=f"Week {report.week} lineup: no data to check it against",
            lines=[
                f"{report.roster_size} rostered player(s), "
                f"{report.projected} with a week {report.week} projection.",
                "",
                "Nothing was compared. Run `fcc sync` (and `fcc sync-league`",
                "once Yahoo access is approved) and the check will run again.",
            ],
            job="lineup",
            urgency="normal",
            season=season,
            week=report.week,
        )
    if not report.swaps and not report.warnings:
        return None

    lines = []
    if report.swaps:
        lines.append(f"Projected gain: +{report.gain:.1f} pts (risk mode: {report.risk_mode})")
        lines.append("")
        lines.extend(f"  {s.describe()}" for s in report.swaps)
    if report.warnings:
        lines.append("")
        lines.append("__WARNINGS__")
        lines.extend(f"  {w}" for w in report.warnings)
    lines.append("")
    lines.append("Set these in Yahoo: My Team -> drag into the listed slot.")

    return Notification(
        title=f"Week {report.week} lineup: {len(report.swaps)} change(s) suggested",
        lines=lines,
        job="lineup",
        urgency="high" if report.gain >= 8 else "normal",
        season=season,
        week=report.week,
        payload={"gain": report.gain, "risk_mode": report.risk_mode},
    )
