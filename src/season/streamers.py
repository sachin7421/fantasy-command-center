"""Ranking a streamable position for one week.

A no-kicker league has two slots you realistically churn: defence and tight
end. Nothing in this application addressed either. The waiver report ranks by
REST-OF-SEASON value, which is the wrong question for a slot you intend to
change again next week - a defence you will drop on Tuesday is worth exactly
what it scores on Sunday.

What this can and cannot do is the whole design. Without Yahoo we do not know
who is AVAILABLE, so it cannot say "pick up this free agent". It can say where
the defence you own ranks among all 32 this week, which is the signal that
sends you to look at the wire - and it says that plainly rather than
presenting a league-wide ranking as a pickup list.

On trusting the numbers: `tools/backtest.py` measured projection error against
2025 results for QB, RB, WR and TE. There were ZERO paired defence
player-weeks, so the accuracy of a defence projection has never been measured
here. No recommendation threshold is invented on that basis - the gap is
reported and the reader decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.storage import Database
from src.yahoo_snapshot import key_clause

#: Positions worth churning weekly. Kicker is absent because this league has
#: no kicker slot, which is also why defence matters more here than usual.
STREAMABLE = ("DEF", "TE")


@dataclass(frozen=True)
class Option:
    player_key: str
    name: str
    team: str
    points: float
    is_mine: bool = False
    #: Where this player sits in the whole field. On the OPTION, not on the
    #: report: owning two at a position made the second one display the
    #: first one's rank, and a bench tight end shown as fourth best in the
    #: league is a reason to start him.
    rank: int = 0


@dataclass
class StreamReport:
    position: str
    week: int
    options: list[Option] = field(default_factory=list)
    my_option: Option | None = None
    my_rank: int | None = None
    #: Everyone at this position with a projection, not just the ones shown.
    #: Using the truncated list made a top-12 display report "2 of 12" when
    #: there are 32 defences - a different claim entirely.
    field_size: int = 0

    @property
    def has_data(self) -> bool:
        return bool(self.options)

    @property
    def gain(self) -> float | None:
        """Points between the best in the league and the one you own.

        None when you own none - there is no gain to state, and reporting 0.0
        would read as "you already have the best".
        """
        if self.my_option is None or not self.options:
            return None
        return round(self.options[0].points - self.my_option.points, 2)

    @property
    def caveat(self) -> str:
        return (
            "Ranked across every team in the league - availability is unknown "
            "without Yahoo, so check who is actually free before acting."
        )

    def describe(self) -> list[str]:
        if not self.has_data:
            return [f"No week {self.week} projections for any {self.position}."]

        lines = []
        if self.my_option is not None and self.my_rank is not None:
            lines.append(
                f"You have {self.my_option.name} - {self.my_rank} of "
                f"{self.field_size} this week ({self.my_option.points:.1f} proj)."
            )
            gain = self.gain
            if gain and gain > 0:
                from src.analytics.uncertainty import (
                    difference_sd,
                    is_meaningful,
                    is_measured,
                )

                best = self.options[0]
                noise = difference_sd(self.position, self.position)
                lines.append(
                    f"Best in the league is {best.name} at {best.points:.1f}, "
                    f"a {gain:.1f} point difference."
                )
                if not is_meaningful(gain, noise):
                    note = (
                        f"That gap is inside the noise - two {self.position} "
                        f"projections differ by about {noise:.0f} points from "
                        "chance alone. Not a reason to move."
                    )
                    if not is_measured(self.position):
                        note += (
                            f" ({self.position} projection accuracy has never "
                            "been measured here; this uses the pooled figure.)"
                        )
                    lines.append(note)
            else:
                lines.append("That is the best projected this week.")
        lines.append(self.caveat)
        return lines


def rank(
    conn: Database,
    season: int,
    week: int,
    position: str,
    mine: set[str] | None = None,
    limit: int = 12,
) -> StreamReport:
    """Every player at `position` with a projection for `week`, best first."""
    mine = {str(k) for k in (mine or set())}
    rows = conn.fetchall(
        "SELECT p.player_key, p.full_name, p.team, b.points "
        "FROM players p JOIN projections_blended b USING(player_key) "
        "WHERE p.position=? AND b.season=? AND b.week=? AND b.points IS NOT NULL "
        "ORDER BY b.points DESC",
        (position.upper(), int(season), int(week)),
    )

    report = StreamReport(
        position=position.upper(), week=int(week), field_size=len(rows)
    )
    for index, row in enumerate(rows, 1):
        option = Option(
            player_key=row["player_key"],
            name=row["full_name"],
            team=row["team"] or "",
            points=float(row["points"]),
            is_mine=row["player_key"] in mine,
            rank=index,
        )
        # The one you own is kept whatever its rank - the answer is useless if
        # it drops off the list at number 13, which is exactly when you most
        # need to know.
        if index <= limit or option.is_mine:
            report.options.append(option)
        if option.is_mine and report.my_option is None:
            report.my_option = option
            report.my_rank = index
    return report


def my_keys_at(conn: Database, snapshot, team_key: str, position: str) -> set[str]:
    """Which of my players are at this position."""
    if snapshot is None:
        return set()
    keys = snapshot.roster_keys(team_key)
    if not keys:
        return set()
    clause, params = key_clause(keys)
    rows = conn.fetchall(
        f"SELECT player_key FROM players "
        f"WHERE position=? AND player_key IN ({clause})",
        (position.upper(), *params),
    )
    return {r["player_key"] for r in rows}
