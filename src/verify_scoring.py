"""Protocol D: does the scoring engine reproduce Yahoo's listed weekly points?

The spec's first acceptance criterion, and the one that could not be checked
until Yahoo data access arrived on 7 Oct 2026. Yahoo's numbers are fetched
live, compared in memory, printed and dropped: the API agreement forbids
persisting them, so there is no fixture and no table - only this check,
re-runnable any week with `fcc verify-scoring`.

What each verdict means:

  exact       the engine's points_actual equals Yahoo's total to the cent
  differs     both have a number and they disagree - a scoring bug, or a
              category the ingest does not carry
  inactive    Yahoo lists 0.00 and nflverse has no row: he did not play
  no-row      Yahoo lists points and nflverse has no row: a sync or id gap
"""
from __future__ import annotations

from typing import Any, NamedTuple

from src.idmap import make_player_key
from src.storage import Database

#: Yahoo publishes totals to two decimals.
TOLERANCE = 0.005


class Comparison(NamedTuple):
    week: int
    name: str
    position: str
    yahoo: float
    ours: float | None
    verdict: str


def _position(player: dict[str, Any]) -> str:
    shown = str(player.get("display_position") or "")
    if shown in ("QB", "RB", "WR", "TE", "DEF"):
        return shown
    return str(player.get("primary_position") or shown)


def compare_roster_points(
    conn: Database, yahoo: Any, *, team_id: int, season: int, weeks: list[int],
) -> list[Comparison]:
    """One row per player on the team's roster in each week, judged as above."""
    out: list[Comparison] = []
    for week in weeks:
        for player in yahoo.fetch_roster_points(team_id, week):
            position = _position(player)
            name = str((player.get("name") or {}).get("full") or "")
            listed = float((player.get("player_points") or {}).get("total") or 0.0)
            # A defense is keyed by its team, the same way the players table
            # keys it; Yahoo gives the team as "Det", the key wants "DET".
            key = make_player_key(name, position, team=player.get("editorial_team_abbr"))
            row = conn.fetchone(
                "SELECT points FROM player_week_actuals WHERE player_key=? "
                "AND season=? AND week=? AND source='nflverse'",
                (key, season, week),
            )
            if row is None:
                verdict = "inactive" if abs(listed) < TOLERANCE else "no-row"
                out.append(Comparison(week, name, position, listed, None, verdict))
                continue
            ours = float(row["points"])
            verdict = "exact" if abs(ours - listed) < TOLERANCE else "differs"
            out.append(Comparison(week, name, position, listed, ours, verdict))
    return out


def summarize(rows: list[Comparison], minimum_exact: int) -> tuple[bool, str]:
    """Pass/fail and a one-paragraph account. The spec asks for ten players;
    fewer exact matches than that proves less than it asks for, and any
    difference at all is a scoring defect to explain, not a rounding error."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    exact = counts.get("exact", 0)
    differs = counts.get("differs", 0)
    ok = exact >= minimum_exact and differs == 0
    parts = [f"{exact} exact", f"{differs} differ"]
    for verdict in ("inactive", "no-row"):
        if counts.get(verdict):
            parts.append(f"{counts[verdict]} {verdict}")
    text = f"{len(rows)} player-weeks: " + ", ".join(parts)
    if exact < minimum_exact:
        text += f" - fewer than the {minimum_exact} exact matches the spec requires"
    return ok, text


def format_rows(rows: list[Comparison]) -> str:
    """The table `fcc verify-scoring` prints: differences and gaps first."""
    order = {"differs": 0, "no-row": 1, "inactive": 2, "exact": 3}
    lines = []
    for row in sorted(rows, key=lambda r: (order.get(r.verdict, 9), r.week, r.name)):
        ours = "-" if row.ours is None else f"{row.ours:7.2f}"
        lines.append(
            f"  wk{row.week} {row.position:<3} {row.name[:26]:<26} "
            f"yahoo={row.yahoo:7.2f} ours={ours:>7}  {row.verdict}"
        )
    return "\n".join(lines)
