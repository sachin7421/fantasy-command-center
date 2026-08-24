"""Tuesday waiver analyzer (spec 6.1).

Ranks free agents by rest-of-season value against my worst droppable player,
proposes explicit ADD/DROP pairs, and - when the league uses FAAB, as this one
does - suggests a bid range rather than a single number.

Sleeper trending adds are the leading indicator here: they show what the wider
fantasy world is claiming *before* the managers in this league react, which is
exactly the edge a Tuesday-morning run is trying to capture.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import logging

from src.notify import Notification
from src.sources.sleeper import severity_of

log = logging.getLogger(__name__)

#: Statuses that make a player a stash rather than a starter.
STASH_STATUSES = {"IR", "PUP", "Out", "Doubtful", "Suspended"}


@dataclass
class Candidate:
    player_key: str
    name: str
    position: str
    team: str
    ros_points: float
    pct_owned: float
    trending_add: int
    injury_status: str | None
    bye_week: int | None

    @property
    def is_stash(self) -> bool:
        return (self.injury_status or "") in STASH_STATUSES


@dataclass
class Claim:
    add: Candidate
    drop: Candidate | None
    value_gain: float
    bid_min: int | None = None
    bid_rec: int | None = None
    bid_max: int | None = None
    priority: int = 0
    reasons: list[str] = field(default_factory=list)

    def describe(self, uses_faab: bool) -> str:
        head = f"ADD {self.add.name} ({self.add.position} {self.add.team})"
        if self.drop:
            head += f"  /  DROP {self.drop.name} ({self.drop.position})"
        head += f"   +{self.value_gain:.1f} ROS pts"
        if uses_faab and self.bid_rec is not None:
            head += f"\n    bid: ${self.bid_min}-${self.bid_max}, recommend ${self.bid_rec}"
        if self.reasons:
            head += "\n    " + "; ".join(self.reasons)
        return head


@dataclass
class WaiverReport:
    claims: list[Claim] = field(default_factory=list)
    stashes: list[Claim] = field(default_factory=list)
    handcuffs: list[dict[str, Any]] = field(default_factory=list)
    #: Buy-low targets and sell-high holdings from expected-points regression.
    buy_low: list[Any] = field(default_factory=list)
    sell_high: list[Any] = field(default_factory=list)
    uses_faab: bool = True
    budget_left: int = 100
    week: int = 0


def _ros_weeks(week: int, final_week: int = 17) -> int:
    return max(1, final_week - week + 1)


def load_free_agents(
    conn: sqlite3.Connection, league_key: str, season: int, week: int, limit: int = 200
) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT f.player_key, f.pct_owned, p.full_name, p.position, p.team, p.bye_week,
               COALESCE(b.points, j.points, 0) AS pts,
               COALESCE(t.count, 0)            AS trending,
               i.status                        AS injury_status
        FROM free_agents f
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key=f.player_key AND b.season=:season AND b.week=:week
        LEFT JOIN projections j
               ON j.player_key=f.player_key AND j.season=:season AND j.week=:week
              AND j.source='sleeper'
        LEFT JOIN (
            SELECT player_key, count,
                   ROW_NUMBER() OVER (PARTITION BY player_key ORDER BY fetched_at DESC) rn
            FROM trending WHERE kind='add'
        ) t ON t.player_key=f.player_key AND t.rn=1
        LEFT JOIN (
            SELECT player_key, status,
                   ROW_NUMBER() OVER (PARTITION BY player_key ORDER BY observed_at DESC) rn
            FROM injuries
        ) i ON i.player_key=f.player_key AND i.rn=1
        WHERE f.league_key=:league AND f.week=:week
        ORDER BY pts DESC
        LIMIT :limit
        """,
        {
            "season": season, "week": week, "league": league_key, "limit": limit,
        },
    ).fetchall()
    return [
        Candidate(
            player_key=r["player_key"], name=r["full_name"], position=r["position"],
            team=r["team"] or "FA", ros_points=float(r["pts"] or 0),
            pct_owned=float(r["pct_owned"] or 0), trending_add=int(r["trending"] or 0),
            injury_status=r["injury_status"], bye_week=r["bye_week"],
        )
        for r in rows
    ]


def load_my_droppables(
    conn: sqlite3.Connection, league_key: str, team_key: str, season: int, week: int
) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT r.player_key, p.full_name, p.position, p.team, p.bye_week,
               COALESCE(b.points, j.points, 0) AS pts,
               i.status AS injury_status
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
        WHERE r.league_key=:league AND r.team_key=:team AND r.week=:week
        ORDER BY pts ASC
        """,
        {"season": season, "week": week, "league": league_key, "team": str(team_key)},
    ).fetchall()
    return [
        Candidate(
            player_key=r["player_key"], name=r["full_name"], position=r["position"],
            team=r["team"] or "", ros_points=float(r["pts"] or 0), pct_owned=100.0,
            trending_add=0, injury_status=r["injury_status"], bye_week=r["bye_week"],
        )
        for r in rows
    ]


def suggest_bid(
    value_gain: float,
    trending_add: int,
    budget_left: int,
    weeks_left: int,
    max_gain_seen: float,
) -> tuple[int, int, int]:
    """FAAB bid range as (min competitive, recommended, max sane).

    Three forces: how much the player improves my team, how hot he is across the
    wider fantasy world (a proxy for how many rivals want him), and how much
    budget is still useful given the weeks remaining.
    """
    if max_gain_seen <= 0:
        return 0, 0, 0

    # Value share: this player relative to the best thing available this week.
    share = max(0.0, min(1.0, value_gain / max_gain_seen))

    # Competition premium from trending velocity, saturating so a viral add does
    # not consume the whole budget.
    heat = min(1.0, trending_add / 50_000) if trending_add else 0.0

    # Budget urgency: money unspent in week 15 is wasted money.
    urgency = 1.0 + max(0.0, (18 - weeks_left)) / 18 * 0.6

    core = share * (0.35 + 0.45 * heat) * urgency
    recommended = int(round(budget_left * core))
    recommended = max(1 if value_gain > 0 else 0, min(recommended, budget_left))

    low = max(0, int(round(recommended * 0.55)))
    high = min(budget_left, max(recommended + 1, int(round(recommended * 1.8))))
    return low, recommended, high


def find_handcuffs(
    conn: sqlite3.Connection, league_key: str, team_key: str, week: int
) -> list[dict[str, Any]]:
    """For each RB I roster, is his backup available? (spec 6.1)

    Uses Sleeper depth-chart order: the same team, same position, next man up.
    """
    my_rbs = conn.execute(
        """
        SELECT p.player_key, p.full_name, p.team
        FROM rosters r JOIN players p USING(player_key)
        WHERE r.league_key=? AND r.team_key=? AND r.week=? AND p.position='RB'
        """,
        (league_key, str(team_key), week),
    ).fetchall()

    out = []
    for rb in my_rbs:
        if not rb["team"]:
            continue
        backup = conn.execute(
            """
            SELECT p.player_key, p.full_name,
                   EXISTS(SELECT 1 FROM rosters r2
                          WHERE r2.player_key=p.player_key AND r2.league_key=?
                            AND r2.week=?) AS rostered
            FROM players p
            WHERE p.team=? AND p.position='RB' AND p.player_key<>?
            ORDER BY (SELECT COALESCE(points,0) FROM projections
                      WHERE player_key=p.player_key AND source='sleeper'
                      ORDER BY season DESC LIMIT 1) DESC
            LIMIT 1
            """,
            (league_key, week, rb["team"], rb["player_key"]),
        ).fetchone()
        if backup:
            out.append(
                {
                    "starter": rb["full_name"],
                    "team": rb["team"],
                    "handcuff": backup["full_name"],
                    "handcuff_key": backup["player_key"],
                    "rostered": bool(backup["rostered"]),
                }
            )
    return out


def run(
    conn: sqlite3.Connection,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
    uses_faab: bool = True,
    budget_left: int = 100,
    value_margin: float = 8.0,
    top_n: int = 8,
) -> WaiverReport:
    free_agents = load_free_agents(conn, league_key, season, week)
    droppables = load_my_droppables(conn, league_key, team_key, season, week)
    weeks_left = _ros_weeks(week)

    worst = droppables[0] if droppables else None
    report = WaiverReport(uses_faab=uses_faab, budget_left=budget_left, week=week)

    # Learn how this league bids, so recommendations reflect the actual rivals
    # rather than a generic rule of thumb.
    profiles: dict[str, Any] = {}
    if uses_faab:
        try:
            from src.analytics import faab as faab_model

            records = faab_model.parse_bids(conn, league_key)
            if records:
                faab_model.attach_values(conn, records, season)
            names = {
                str(r["team_key"]): (r["team_name"] or str(r["team_key"]))
                for r in conn.fetchall(
                    "SELECT DISTINCT team_key, team_name FROM rosters "
                    "WHERE league_key=?",
                    (league_key,),
                )
            }
            if records or names:
                profiles = faab_model.learn_profiles(records, names)
        except Exception as exc:  # pragma: no cover - optional enrichment
            log.info("FAAB profiles unavailable: %s", exc)

    healthy = [c for c in free_agents if not c.is_stash]
    gains = [
        c.ros_points - (worst.ros_points if worst else 0.0) for c in healthy
    ]
    max_gain = max(gains) if gains else 0.0

    used_drops: set[str] = set()
    for candidate in healthy:
        base = worst.ros_points if worst else 0.0
        gain = candidate.ros_points - base
        if gain < value_margin:
            continue

        drop = next((d for d in droppables if d.player_key not in used_drops), None)
        if drop:
            used_drops.add(drop.player_key)

        reasons = []
        if candidate.trending_add > 5000:
            reasons.append(f"trending: {candidate.trending_add:,} adds in 24h")
        if candidate.pct_owned < 40 and candidate.ros_points > base:
            reasons.append(f"only {candidate.pct_owned:.0f}% rostered")
        if drop and drop.injury_status in STASH_STATUSES:
            reasons.append(f"drop candidate is {drop.injury_status}")

        claim = Claim(
            add=candidate,
            drop=drop,
            value_gain=round(gain, 1),
            reasons=reasons,
        )
        if uses_faab:
            # Prefer a bid derived from how this league ACTUALLY bids, and fall
            # back to the heuristic until enough auctions have been observed.
            advice = None
            if profiles:
                from src.analytics import faab as faab_model

                rivals = [
                    p for key, p in profiles.items() if key != str(team_key)
                ]
                advice = faab_model.recommend(
                    value=gain * 0.12, my_budget=budget_left,
                    rivals=rivals, weeks_left=weeks_left,
                )
            if advice and advice.recommended:
                claim.bid_min = advice.min_competitive
                claim.bid_rec = advice.recommended
                claim.bid_max = advice.max_sane
                if advice.win_probability:
                    claim.reasons.append(
                        f"{advice.win_probability:.0%} to win at ${advice.recommended} "
                        f"given how this league bids"
                    )
            else:
                claim.bid_min, claim.bid_rec, claim.bid_max = suggest_bid(
                    gain, candidate.trending_add, budget_left, weeks_left, max_gain
                )
        report.claims.append(claim)

    report.claims.sort(key=lambda c: c.value_gain, reverse=True)
    report.claims = report.claims[:top_n]
    for i, claim in enumerate(report.claims, 1):
        claim.priority = i

    # Stashes: injured talent worth holding, and unrostered handcuffs.
    for candidate in free_agents:
        if candidate.is_stash and candidate.ros_points > 0:
            report.stashes.append(
                Claim(
                    add=candidate, drop=None, value_gain=round(candidate.ros_points, 1),
                    reasons=[f"stash: {candidate.injury_status}"],
                )
            )
    report.stashes = report.stashes[:5]
    report.handcuffs = [
        h for h in find_handcuffs(conn, league_key, team_key, week) if not h["rostered"]
    ]

    # Expected-points regression. The waiver wire is exactly where this pays:
    # a free agent scoring below what his usage implies is the cheapest player
    # on the board, and one of your own scoring above it is the one to move
    # before the market notices.
    try:
        from src.analytics import regression

        signals = regression.scan(conn, season, through_week=week)
        available = {c.player_key for c in free_agents}
        mine = {
            r["player_key"]
            for r in conn.fetchall(
                "SELECT player_key FROM rosters WHERE league_key=? AND team_key=? "
                "AND week=?",
                (league_key, str(team_key), week),
            )
        }
        report.buy_low = [
            s for s in signals if s.verdict == "buy" and s.player_key in available
        ][:5]
        report.sell_high = [
            s for s in signals if s.verdict == "sell" and s.player_key in mine
        ][:5]
    except Exception as exc:  # pragma: no cover - analytics are optional
        log.info("regression signals unavailable: %s", exc)

    return report


def to_notification(report: WaiverReport, season: int) -> Notification | None:
    if not (report.claims or report.stashes or report.handcuffs):
        return None

    lines: list[str] = []
    if report.claims:
        budget = f" (FAAB left: ${report.budget_left})" if report.uses_faab else ""
        lines.append(f"__CLAIMS{budget}__")
        for c in report.claims:
            lines.append(f"  {c.priority}. {c.describe(report.uses_faab)}")
        lines.append("")
    if report.stashes:
        lines.append("__STASH CANDIDATES__")
        lines.extend(f"  {s.add.name} ({s.add.position}) - {s.reasons[0]}" for s in report.stashes)
        lines.append("")
    if report.buy_low:
        lines.append("__BUY LOW - available and underperforming their usage__")
        for s in report.buy_low:
            lines.append(
                f"  {s.name} ({s.position} {s.team}): scoring {abs(s.residual):.1f} "
                f"pts/gm below what his usage implies over {s.games} games"
            )
        lines.append("")
    if report.sell_high:
        lines.append("__SELL HIGH - yours, and outscoring their usage__")
        for s in report.sell_high:
            lines.append(
                f"  {s.name} ({s.position} {s.team}): scoring {s.residual:.1f} "
                f"pts/gm above what his usage implies - trade while the price holds"
            )
        lines.append("")
    if report.handcuffs:
        lines.append("__UNROSTERED HANDCUFFS__")
        lines.extend(
            f"  {h['handcuff']} backs up your {h['starter']} ({h['team']})"
            for h in report.handcuffs
        )
        lines.append("")
    lines.append("Submit in Yahoo before waivers process (Tue game time).")

    return Notification(
        title=f"Week {report.week} waivers: {len(report.claims)} claim(s)",
        lines=lines,
        job="waivers",
        urgency="normal",
        season=season,
        week=report.week,
        payload={"claims": len(report.claims)},
    )
