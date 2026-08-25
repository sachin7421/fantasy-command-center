"""Tuesday waiver analyzer (spec 6.1).

Ranks free agents by rest-of-season value against my worst droppable player,
proposes explicit ADD/DROP pairs, and - when the league uses FAAB, as this one
does - suggests a bid range rather than a single number.

Sleeper trending adds are the leading indicator here: they show what the wider
fantasy world is claiming *before* the managers in this league react, which is
exactly the edge a Tuesday-morning run is trying to capture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import logging

from src.notify import Notification
from src.storage import Database

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
    #: Points above what is freely available at this position. Raw points are
    #: not comparable across positions - a defence projecting 110 is not worse
    #: than a running back projecting 150 - and comparing them directly made
    #: the "worst player on your roster" the defence every single week, so the
    #: job kept proposing you drop your only one to add a backup quarterback.
    value: float = 0.0

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


#: Last week of the fantasy regular season. This lived as three different
#: literals - `18 - week` here, `15 - week` in the FAAB command, and a `14`
#: inside the urgency multiplier - so `fcc waivers` and `fcc faab <player>`
#: priced the same player about 10% apart, and a week-13 add for the title run
#: was valued as though two weeks remained.
FINAL_WEEK = 17


def ros_fraction(week: int, final_week: int = FINAL_WEEK) -> float:
    """How much of a season projection is still ahead of you.

    A week-0 projection covers the whole season. Treating it as
    rest-of-season value in week 12 overstates a claim by roughly 2.4x, and
    that inflated number is what the bid model was handed - so a routine add
    in November was priced like a league-winner in September.
    """
    return max(0.0, min(1.0, _ros_weeks(week, final_week) / float(final_week)))


def _ros_weeks(week: int, final_week: int = FINAL_WEEK) -> int:
    return max(1, final_week - week + 1)


def replacement_levels(
    conn: Database,
    season: int,
    league_key: str | None = None,
    week: int | None = None,
) -> dict[str, float]:
    """Positional replacement level, shared with the FAAB model.

    Deliberately the same function the bid model trains on, so a claim's value
    and the bid recommended for it are measured the same way.
    """
    from src.analytics.faab import replacement_levels as _levels

    return _levels(conn, season, league_key=league_key, week=week)


def load_free_agents(
    conn: Database, league_key: str, season: int, week: int, limit: int = 200
) -> list[Candidate]:
    """Available players, valued on rest-of-season points.

Both projection joins prefer the SEASON line (week 0) and fall back to the
weekly one. The field is called `ros_points` and the output says "ROS pts", but
the query used to read week N only - so every number here was a single week's
projection wearing a rest-of-season label. That is not just cosmetic: the value
is handed to the FAAB model, whose `ELITE_CLAIM_VALUE` and whose learned rival
bids are both calibrated against season points above replacement. Feeding it a
weekly figure understated every claim by roughly the number of weeks left, and
recommended $1 bids on players worth real money.
    """
    rows = conn.execute(
        """
        SELECT f.player_key, f.pct_owned, p.full_name, p.position, p.team, p.bye_week,
               COALESCE(s.points, b.points, j.points, 0) AS pts,
               COALESCE(t.count, 0)                      AS trending,
               i.status                        AS injury_status
        FROM free_agents f
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended s
               ON s.player_key=f.player_key AND s.season=:season AND s.week=0
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
    baseline = replacement_levels(conn, season)
    share = ros_fraction(week)
    return [
        Candidate(
            player_key=r["player_key"], name=r["full_name"], position=r["position"],
            team=r["team"] or "FA", ros_points=float(r["pts"] or 0),
            pct_owned=float(r["pct_owned"] or 0), trending_add=int(r["trending"] or 0),
            injury_status=r["injury_status"], bye_week=r["bye_week"],
            value=(float(r["pts"] or 0) - baseline.get(r["position"], 0.0)) * share,
        )
        for r in rows
    ]


def load_my_droppables(
    conn: Database, league_key: str, team_key: str, season: int, week: int
) -> list[Candidate]:
    """Your roster, worst first, on the same season scale as the free agents.

    Both sides of the comparison have to be measured the same way or the margin
    is meaningless; see `load_free_agents` for why that is week 0.
    """
    rows = conn.execute(
        """
        SELECT r.player_key, p.full_name, p.position, p.team, p.bye_week,
               COALESCE(s.points, b.points, j.points, 0) AS pts,
               i.status AS injury_status
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended s
               ON s.player_key=r.player_key AND s.season=:season AND s.week=0
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
        """,
        {"season": season, "week": week, "league": league_key, "team": str(team_key)},
    ).fetchall()
    baseline = replacement_levels(conn, season)
    share = ros_fraction(week)
    out = [
        Candidate(
            player_key=r["player_key"], name=r["full_name"], position=r["position"],
            team=r["team"] or "", ros_points=float(r["pts"] or 0), pct_owned=100.0,
            trending_add=0, injury_status=r["injury_status"], bye_week=r["bye_week"],
            value=(float(r["pts"] or 0) - baseline.get(r["position"], 0.0)) * share,
        )
        for r in rows
    ]
    # Worst FIRST, on the comparable scale rather than on raw points.
    out.sort(key=lambda c: c.value)
    return out


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
    urgency = 1.0 + max(0.0, (FINAL_WEEK - weeks_left)) / FINAL_WEEK * 0.6

    core = share * (0.35 + 0.45 * heat) * urgency
    recommended = int(round(budget_left * core))
    # With no budget there is no bid. The old floor forced 1 regardless, and
    # the ceiling then clamped to 0, so a spent manager was told
    # "bid: $1-$0, recommend $1".
    if budget_left <= 0:
        return 0, 0, 0
    recommended = max(1 if value_gain > 0 else 0, min(recommended, budget_left))

    low = max(0, min(recommended, int(round(recommended * 0.55))))
    high = min(budget_left, max(recommended, int(round(recommended * 1.8))))
    return low, recommended, high


def find_handcuffs(
    conn: Database, league_key: str, team_key: str, week: int, season: int | None = None
) -> list[dict[str, Any]]:
    """For each RB I roster, is the man behind him on the depth chart free?

    Genuinely uses depth-chart order now. The docstring said so before, and the
    query ordered by projected points instead - which for a committee backfield
    returns the STARTER, so the tool would tell you the handcuff for your
    backup was the lead back you did not own. `depth_charts` is populated by
    `fcc sync-usage` and was being written and read by nothing.

    Falls back to the old projection ordering only when no depth chart has been
    synced, and says which one it used.
    """
    my_rbs = conn.execute(
        """
        SELECT p.player_key, p.full_name, p.team
        FROM rosters r JOIN players p USING(player_key)
        WHERE r.league_key=? AND r.team_key=? AND r.week=? AND p.position='RB'
        """,
        (league_key, str(team_key), week),
    ).fetchall()

    # Most recent depth chart available, whatever season it came from.
    latest = conn.fetchone("SELECT MAX(season) AS s FROM depth_charts")         if conn.table_exists("depth_charts") else None
    depth_season = latest["s"] if latest and latest["s"] is not None else None
    if season is not None and depth_season is not None:
        depth_season = min(depth_season, season)

    out = []
    for rb in my_rbs:
        if not rb["team"]:
            continue
        backup = None
        source = "depth chart"
        if depth_season is not None:
            backup = conn.fetchone(
                """
                SELECT d.player_key, d.player_name AS full_name,
                       EXISTS(SELECT 1 FROM rosters r2
                              WHERE r2.player_key=d.player_key
                                AND r2.league_key=? AND r2.week=?) AS rostered
                FROM depth_charts d
                WHERE d.season=? AND d.team=? AND d.position='RB'
                  AND d.player_key IS NOT NULL
                  AND d.depth_rank > (
                      SELECT MIN(depth_rank) FROM depth_charts
                      WHERE season=? AND team=? AND position='RB'
                        AND player_key=?
                  )
                ORDER BY d.depth_rank ASC
                LIMIT 1
                """,
                (league_key, week, depth_season, rb["team"],
                 depth_season, rb["team"], rb["player_key"]),
            )
        if backup is None:
            source = "projection order (no depth chart synced)"
            backup = conn.fetchone(
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
            )
        if backup:
            out.append(
                {
                    "starter": rb["full_name"],
                    "team": rb["team"],
                    "handcuff": backup["full_name"],
                    "source": source,
                    "handcuff_key": backup["player_key"],
                    "rostered": bool(backup["rostered"]),
                }
            )
    return out


def _lineup_total(roster: list[Candidate], starting_slots: dict[str, int]) -> float:
    """Points of the best legal starting lineup this roster can field."""
    from src.lineup_solver import best_lineup

    return best_lineup(
        roster,
        starting_slots,
        points_of=lambda c: c.ros_points,
        position_of=lambda c: c.position,
    ).total


def _protected_keys(
    roster: list[Candidate], starting_slots: dict[str, int] | None
) -> set[str]:
    """Players who cannot be dropped without emptying a required starting slot.

    Only the last one at a dedicated position is protected - depth at a position
    is always droppable, and flex slots are covered by several positions, so
    nothing there is irreplaceable.
    """
    if not starting_slots:
        return set()
    from src.vorp import split_slots

    dedicated, _ = split_slots(starting_slots)
    by_position: dict[str, list[Candidate]] = {}
    for player in roster:
        by_position.setdefault(player.position, []).append(player)

    protected: set[str] = set()
    for position, needed in dedicated.items():
        held = by_position.get(position, [])
        if needed > 0 and len(held) <= needed:
            protected.update(p.player_key for p in held)
    return protected


def run(
    conn: Database,
    league_key: str,
    team_key: str,
    season: int,
    week: int,
    uses_faab: bool = True,
    budget_left: int = 100,
    value_margin: float = 25.0,
    top_n: int = 8,
    starting_slots: dict[str, int] | None = None,
) -> WaiverReport:
    free_agents = load_free_agents(conn, league_key, season, week)
    droppables = load_my_droppables(conn, league_key, team_key, season, week)
    weeks_left = _ros_weeks(week)

    # A player who is the last one you own at a required position is not a drop
    # candidate, whatever he projects for. Dropping your only defence to add a
    # fourth quarterback is not an upgrade; it forfeits a starting slot.
    protected = _protected_keys(droppables, starting_slots)
    droppable_now = [d for d in droppables if d.player_key not in protected]

    worst = droppable_now[0] if droppable_now else None
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
            # Remaining budgets are the sharpest input the model has: a
            # manager with $2 left is not a rival whatever his habits. Without
            # them every hard-ceiling branch is skipped and the feature is inert.
            balances = {
                str(r["team_key"]): int(r["faab_balance"])
                for r in conn.fetchall(
                    "SELECT team_key, faab_balance FROM team_budgets "
                    "WHERE league_key=? AND season=?",
                    (league_key, season),
                )
                if r["faab_balance"] is not None
            } if conn.table_exists("team_budgets") else {}

            if records or names:
                profiles = faab_model.learn_profiles(records, names, balances)
        except Exception as exc:  # pragma: no cover - optional enrichment
            log.info("FAAB profiles unavailable: %s", exc)

    healthy = [c for c in free_agents if not c.is_stash]

    # What a claim is actually worth is what it does to the STARTING LINEUP.
    # Measuring it against the worst player on the roster instead recommended
    # four backup quarterbacks in a one-QB league: each of them beat the worst
    # bench spot comfortably, and not one of them would ever have started.
    def gain_for(candidate: Candidate, drop: Candidate | None) -> float:
        if not starting_slots:
            return candidate.value - (worst.value if worst else 0.0)
        after = [d for d in droppables if drop is None or d.player_key != drop.player_key]
        after.append(candidate)
        return round(_lineup_total(after, starting_slots) - current_total, 2)

    current_total = (
        _lineup_total(droppables, starting_slots) if starting_slots else 0.0
    )
    gains = [gain_for(c, None) for c in healthy]
    max_gain = max(gains) if gains else 0.0

    used_drops: set[str] = set()
    for candidate in healthy:
        drop_preview = next(
            (d for d in droppable_now if d.player_key not in used_drops), None
        )
        gain = gain_for(candidate, drop_preview)
        if gain < value_margin:
            continue

        drop = drop_preview
        if drop:
            used_drops.add(drop.player_key)

        reasons = []
        if candidate.trending_add > 5000:
            reasons.append(f"trending: {candidate.trending_add:,} adds in 24h")
        if candidate.pct_owned < 40 and gain > 0:
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
                # `gain` is already ROS points above the droppable, which is
                # exactly the scale the model documents. It must NOT be rescaled.
                advice = faab_model.recommend(
                    value=gain, my_budget=budget_left,
                    rivals=rivals, weeks_left=weeks_left,
                )
            if advice and advice.recommended:
                # The three numbers are rendered as a range around the
                # recommendation, so they have to bracket it. When a player is
                # worth less to you than the league will pay, the recommendation
                # sits BELOW the competitive floor, and printing the raw numbers
                # gave "$63-$63, recommend $46" - three true figures arranged
                # into something unreadable. The note below says the same thing
                # in words.
                claim.bid_rec = advice.recommended
                claim.bid_min = min(advice.min_competitive, advice.recommended)
                claim.bid_max = max(advice.price_to_win, advice.recommended)
                if advice.recommended < advice.min_competitive:
                    claim.reasons.append(
                        f"he is worth ${advice.recommended} to you but the league "
                        f"is paying about ${advice.price_to_win}"
                    )
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
        h for h in find_handcuffs(conn, league_key, team_key, week, season)
        if not h["rostered"]
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
