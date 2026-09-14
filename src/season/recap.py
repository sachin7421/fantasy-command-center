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
from src.yahoo_snapshot import key_clause


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
    #: The slot this player actually occupied, so a mistake can be reported
    #: against the slot rather than against an arbitrary other starter.
    slot: str | None = None
    #: Whether a real score was recorded. Distinguishes a genuine zero from a
    #: week that has not been played or synced.
    scored: bool = False


BENCH_SLOTS = ("BN", "IR", "IR+", "NA")


def _was_started(slot: str | None) -> bool:
    """Whether a roster slot is a starting one."""
    return bool(slot) and str(slot).upper() not in BENCH_SLOTS


def _actual_week_scores(
    conn: Database, season: int, week: int, roster_spots
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
    clause, params = key_clause([s.player_key for s in roster_spots])
    slot_of = {s.player_key: s.selected_pos for s in roster_spots}
    rows = conn.execute(
        f"""
        SELECT p.player_key, p.full_name, p.position,
               a.points AS actual_pts,
               b.points AS projected_pts
        FROM players p
        LEFT JOIN player_week_actuals a
               ON a.player_key=p.player_key AND a.season=? AND a.week=?
        LEFT JOIN projections_blended b
               ON b.player_key=p.player_key AND b.season=? AND b.week=?
        WHERE p.player_key IN ({clause})
        """,
        (season, week, season, week, *params),
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
            # The slot comes from the snapshot, not from the row - the query
            # no longer selects it. A player with no recorded slot is treated
            # as benched rather than started: crediting an unknown as a starter
            # would inflate what the lineup actually scored.
            started=_was_started(slot_of.get(r["player_key"])),
            slot=slot_of.get(r["player_key"]),
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
    snapshot=None,
) -> RecapReport:
    """Last week, scored - what you started, what you should have.

    `snapshot` carries the roster. Yahoo league state is fetched once per run
    and passed in rather than read from a table, which the agreement no longer
    permits.
    """
    if snapshot is None:
        raise ValueError("recap needs a league snapshot: the roster comes from Yahoo")
    roster = _actual_week_scores(
        conn, season, week, snapshot.roster_spots_for(team_key)
    )
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

    # Compared SLOT BY SLOT, not by zipping two sorted lists.
    #
    # The old version paired the best benched player against the worst
    # wrongly-started one with no check that either could take the other's
    # slot, and sent this out: "Jordan Love (22.5) on bench while Rome Odunze
    # (6.2) started, -16.3". Love is a quarterback; Odunze started at W/R/T. A
    # QB cannot occupy a flex, so that is an instruction nobody could have
    # executed - it blamed the manager for a decision he was never able to
    # make. It also produced "-3.3" costs: a benched player who scored LESS
    # than the starter, reported as a mistake.
    #
    # The optimal lineup is already slot-assigned and already legal, so
    # walking it against what was actually started can only ever produce a
    # swap that was possible.
    actual_by_slot: dict[str, list[_Scored]] = {}
    for player in started:
        actual_by_slot.setdefault((player.slot or "").upper(), []).append(player)

    for assigned in optimal.slots:
        candidate = assigned.player
        if candidate is None:
            continue
        slot = (assigned.slot or "").upper()
        occupants = actual_by_slot.get(slot) or []
        if not occupants:
            continue
        # Whoever actually held this slot and is not the optimal choice.
        displaced = next(
            (p for p in occupants if p.player_key != candidate.player_key), None
        )
        if displaced is None:
            continue
        occupants.remove(displaced)
        # Only a LOSS is a mistake. A swap that would have scored fewer points
        # is not something the manager got wrong.
        if candidate.points <= displaced.points:
            continue
        report.mistakes.append(
            BenchMistake(
                benched=candidate.name,
                benched_points=candidate.points,
                started=displaced.name,
                started_points=displaced.points,
            )
        )

    report.mistakes.sort(key=lambda m: m.cost, reverse=True)
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
            # "-{cost}" rendered as "--3.3" whenever cost was negative, which
            # it could be before mistakes were restricted to actual losses.
            # Saying "cost you N" removes the ambiguity entirely: a reader
            # should not have to work out whether a minus sign is a subtraction
            # or a direction.
            lines.append(
                f"  {m.benched} ({m.benched_points:.1f}) on the bench, "
                f"{m.started} ({m.started_points:.1f}) started in his slot "
                f"- cost you {m.cost:.1f}"
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
