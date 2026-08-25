"""Daily injury & news monitor (spec 6.2).

Diffs today's injury statuses against the last snapshot and alerts only on
*changes* that touch something I care about:
  (a) my roster, (b) my opponent's key players, (c) top free agents.

Alerting on state rather than on change is what makes these tools unusable, so
the snapshot diff is the whole point: an unchanged Questionable tag is silent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable

from src import db
from src.notify import Notification
from src.sources.sleeper import is_escalation, severity_of
from src.storage import Database

SNAPSHOT_KIND = "injuries"


@dataclass
class StatusChange:
    player_key: str
    name: str
    position: str
    team: str
    before: str | None
    after: str | None
    body_part: str | None
    context: str          # roster | opponent | free_agent | watch
    replacement: str | None = None
    #: Practice participation from the official report, when available.
    practice: str | None = None

    @property
    def is_escalation(self) -> bool:
        return is_escalation(self.before, self.after)

    @property
    def is_recovery(self) -> bool:
        return severity_of(self.after) < severity_of(self.before)

    def describe(self) -> str:
        before = self.before or "Healthy"
        after = self.after or "Healthy"
        arrow = "->"
        detail = f" ({self.body_part})" if self.body_part else ""
        line = f"{self.name} {self.position} {self.team}: {before} {arrow} {after}{detail}"
        if self.practice:
            line += f"\n    practice: {self.practice}"
        if self.replacement:
            line += f"\n    replacement: {self.replacement}"
        return line


@dataclass
class InjuryReport:
    changes: list[StatusChange] = field(default_factory=list)
    checked: int = 0
    first_run: bool = False
    #: The statuses to store once this report has actually been delivered.
    #: Held rather than written so a failed send does not consume the delta.
    pending_snapshot: dict[str, str] = field(default_factory=dict)


    @property
    def actionable(self) -> list[StatusChange]:
        return [c for c in self.changes if c.context in ("roster", "opponent", "free_agent")]

    @property
    def escalations(self) -> list[StatusChange]:
        return [c for c in self.changes if c.is_escalation]


def current_statuses(conn: Database) -> dict[str, dict[str, Any]]:
    """Latest known injury status for every player carrying one."""
    rows = conn.execute(
        """
        SELECT i.player_key, i.status, i.body_part, p.full_name, p.position, p.team
        FROM injuries i
        JOIN players p USING(player_key)
        JOIN (
            SELECT player_key, MAX(observed_at) AS latest
            FROM injuries GROUP BY player_key
        ) m ON m.player_key = i.player_key AND m.latest = i.observed_at
        """
    ).fetchall()
    return {
        r["player_key"]: {
            "status": r["status"], "body_part": r["body_part"],
            "name": r["full_name"], "position": r["position"], "team": r["team"],
        }
        for r in rows
    }


def my_roster_keys(conn: Database, league_key: str, team_key: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT player_key FROM rosters WHERE league_key=? AND team_key=? "
        "AND week=(SELECT MAX(week) FROM rosters WHERE league_key=?)",
        (league_key, str(team_key), league_key),
    ).fetchall()
    return {r["player_key"] for r in rows}


def opponent_roster_keys(
    conn: Database, league_key: str, opponent_team_key: str | None
) -> set[str]:
    if not opponent_team_key:
        return set()
    return my_roster_keys(conn, league_key, opponent_team_key)


def top_free_agent_keys(
    conn: Database, league_key: str, limit: int = 60
) -> set[str]:
    rows = conn.execute(
        """
        SELECT f.player_key
        FROM free_agents f
        LEFT JOIN projections_blended b ON b.player_key = f.player_key
        WHERE f.league_key = ?
        ORDER BY COALESCE(b.points, f.pct_owned, 0) DESC
        LIMIT ?
        """,
        (league_key, limit),
    ).fetchall()
    return {r["player_key"] for r in rows}


def practice_note(conn, player_key: str, season: int, week: int) -> str | None:
    """Practice participation for the week, when the official report has it.

    This is the part the Sleeper feed does not carry, and it is the more
    predictive half: a player who did not practise on Wednesday and Thursday is
    far likelier to sit than his game-status tag alone suggests - and it is
    knowable midweek rather than at kickoff.
    """
    row = conn.fetchone(
        "SELECT report_status, practice_status, primary_injury "
        "FROM practice_reports WHERE player_key=? AND season=? AND week=?",
        (player_key, season, week),
    )
    if not row or not row["practice_status"]:
        return None
    status = str(row["practice_status"])
    short = (
        "did not practise" if "Did Not" in status
        else "limited in practice" if "Limited" in status
        else "full practice"
    )
    injury = row["primary_injury"]
    return f"{short}{f' ({injury})' if injury else ''}"


def best_bench_replacement(
    conn: Database,
    league_key: str,
    team_key: str,
    position: str,
    season: int,
    week: int,
    exclude: str,
) -> str | None:
    """The best same-position player already on my bench."""
    row = conn.execute(
        """
        SELECT p.full_name, COALESCE(b.points, 0) AS pts
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key = r.player_key AND b.season=? AND b.week=?
        WHERE r.league_key=? AND r.team_key=? AND p.position=? AND r.player_key<>?
        ORDER BY pts DESC LIMIT 1
        """,
        (season, week, league_key, str(team_key), position, exclude),
    ).fetchone()
    return f"{row['full_name']} ({row['pts']:.1f} proj)" if row else None


def run(
    conn: Database,
    league_key: str,
    my_team_key: str | None,
    season: int,
    week: int,
    opponent_team_key: str | None = None,
    watch_keys: Iterable[str] = (),
    commit_snapshot: bool = True,
) -> InjuryReport:
    """Snapshot, diff against yesterday, and classify every change.

    The new snapshot is NOT written here. Advancing it before the notification
    is delivered makes the report single-use: a re-run, an SMTP failure, or the
    daily path running this job twice consumed the delta and the alert was gone
    for good. `--dry-run` had the same effect, so an injury report could not be
    previewed without destroying it.

    The caller commits it with `commit(conn, report)` once delivery succeeds.
    """
    statuses = current_statuses(conn)
    previous = db.snapshot_latest(conn, SNAPSHOT_KIND)

    report = InjuryReport(checked=len(statuses), first_run=previous is None)
    report.pending_snapshot = {k: v["status"] for k, v in statuses.items()}
    if previous is None:
        # Nothing to diff against yet; establish the baseline silently rather
        # than firing an alert for every currently-injured player in the NFL.
        return report

    roster = my_roster_keys(conn, league_key, my_team_key) if my_team_key else set()
    opponents = opponent_roster_keys(conn, league_key, opponent_team_key)
    free_agents = top_free_agent_keys(conn, league_key)
    watching = set(watch_keys)

    considered = set(previous) | set(statuses)
    for player_key in considered:
        before = previous.get(player_key)
        after = statuses.get(player_key, {}).get("status")
        if before == after:
            continue

        if player_key in roster:
            context = "roster"
        elif player_key in opponents:
            context = "opponent"
        elif player_key in free_agents:
            context = "free_agent"
        elif player_key in watching:
            context = "watch"
        else:
            continue  # not my problem

        info = statuses.get(player_key, {})
        name = info.get("name")
        if not name:
            row = conn.execute(
                "SELECT full_name, position, team FROM players WHERE player_key=?",
                (player_key,),
            ).fetchone()
            if not row:
                continue
            info = {"name": row["full_name"], "position": row["position"], "team": row["team"]}

        change = StatusChange(
            player_key=player_key,
            name=info.get("name", player_key),
            position=info.get("position", ""),
            team=info.get("team", "") or "FA",
            before=before,
            after=after,
            body_part=info.get("body_part"),
            context=context,
        )
        if context == "roster" and change.is_escalation and my_team_key:
            change.replacement = best_bench_replacement(
                conn, league_key, my_team_key, change.position, season, week, player_key
            )
        change.practice = practice_note(conn, player_key, season, week)
        report.changes.append(change)

    report.changes.sort(
        key=lambda c: (
            {"roster": 0, "free_agent": 1, "opponent": 2, "watch": 3}[c.context],
            -severity_of(c.after),
        )
    )
    return report


def commit(conn: Database, report: InjuryReport) -> None:
    """Advance the baseline. Call only after the report has been delivered."""
    if report.pending_snapshot:
        db.snapshot_put(conn, SNAPSHOT_KIND, report.pending_snapshot)


def to_notification(report: InjuryReport, week: int, season: int) -> Notification | None:
    """Build the alert, or None when nothing worth saying happened."""
    actionable = report.actionable
    if not actionable:
        return None

    lines: list[str] = []
    for context, label in (
        ("roster", "YOUR ROSTER"),
        ("free_agent", "FREE AGENTS"),
        ("opponent", "OPPONENT"),
    ):
        group = [c for c in actionable if c.context == context]
        if not group:
            continue
        lines.append(f"__{label}__")
        lines.extend(f"  {c.describe()}" for c in group)
        lines.append("")

    roster_escalations = [
        c for c in actionable if c.context == "roster" and c.is_escalation
    ]
    urgency = "high" if roster_escalations else "normal"
    title = (
        f"Injury alert: {len(roster_escalations)} on your roster"
        if roster_escalations
        else f"Injury update: {len(actionable)} changes"
    )
    return Notification(
        title=title,
        lines=lines,
        job="injuries",
        urgency=urgency,
        season=season,
        week=week,
        payload={"changes": [c.__dict__ for c in actionable]},
    )
