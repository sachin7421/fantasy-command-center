"""Game context: what the betting market and the weather imply.

Two inputs that sit outside a player's own stat line but move his projection a
long way.

**Implied team total.** The betting market is the sharpest public forecast of how
many points a team will score, and it is free to read:

    implied_total = total / 2  -  spread / 2

A team favoured by 7 in a game with a 47-point total is implied for 27, its
opponent for 20. That difference is worth several fantasy points to the players
involved. The spread also sets *game script*: a heavy favourite runs the ball to
burn clock, an underdog throws to catch up - so the same total means different
things for a running back than for a receiver.

**Weather.** Mostly irrelevant, occasionally decisive. Wind is the one that
matters: above roughly 15mph it measurably suppresses passing and kicking. Rain
and cold matter far less than folklore suggests, and neither matters at all
indoors, so the dome flag comes first.

Odds need a free key from the-odds-api.com; without one this degrades to weather
only, and without that, to nothing. Neither is required for the app to work.
"""
from __future__ import annotations

import logging
from typing import Any

from src import db
from src.config import env
from src.idmap import normalize_team
from src.sources.base import Source

log = logging.getLogger(__name__)

ODDS_BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
# Open-Meteo needs no key and no account.
WEATHER_BASE = "https://api.open-meteo.com/v1/forecast"

#: Wind above this is where passing and kicking start to suffer measurably.
WIND_CONCERN_MPH = 15.0

#: Stadium coordinates and roof type. Indoor venues skip weather entirely.
#: (latitude, longitude, is_dome)
STADIUMS: dict[str, tuple[float, float, bool]] = {
    "ARI": (33.5277, -112.2626, True),   # retractable, usually closed
    "ATL": (33.7554, -84.4009, True),
    "BAL": (39.2780, -76.6227, False),
    "BUF": (42.7738, -78.7870, False),
    "CAR": (35.2258, -80.8528, False),
    "CHI": (41.8623, -87.6167, False),
    "CIN": (39.0955, -84.5161, False),
    "CLE": (41.5061, -81.6995, False),
    "DAL": (32.7473, -97.0945, True),
    "DEN": (39.7439, -105.0201, False),
    "DET": (42.3400, -83.0456, True),
    "GB":  (44.5013, -88.0622, False),
    "HOU": (29.6847, -95.4107, True),
    "IND": (39.7601, -86.1639, True),
    "JAX": (30.3239, -81.6373, False),
    "KC":  (39.0489, -94.4839, False),
    "LAC": (33.9535, -118.3392, True),
    "LAR": (33.9535, -118.3392, True),
    "LV":  (36.0909, -115.1833, True),
    "MIA": (25.9580, -80.2389, False),
    "MIN": (44.9736, -93.2575, True),
    "NE":  (42.0909, -71.2643, False),
    "NO":  (29.9511, -90.0812, True),
    "NYG": (40.8135, -74.0745, False),
    "NYJ": (40.8135, -74.0745, False),
    "PHI": (39.9008, -75.1675, False),
    "PIT": (40.4468, -80.0158, False),
    "SEA": (47.5952, -122.3316, False),
    "SF":  (37.4033, -121.9694, False),
    "TB":  (27.9759, -82.5033, False),
    "TEN": (36.1665, -86.7713, False),
    "WAS": (38.9077, -76.8645, False),
}


def implied_total(game_total: float | None, spread: float | None) -> float | None:
    """Points a team is expected to score.

    `spread` is from that team's perspective: negative means favoured.
    """
    if game_total is None or spread is None:
        return None
    return round(game_total / 2.0 - spread / 2.0, 2)


def game_script(spread: float | None) -> str:
    """What the spread implies about how a team will play.

    A big favourite runs to drain the clock; a big underdog throws to catch up.
    Same game total, opposite consequences for a back and a receiver.
    """
    if spread is None:
        return "neutral"
    if spread <= -7:
        return "heavy favourite: run-leaning, RB volume up, passing down"
    if spread <= -3:
        return "favourite: mild run lean"
    if spread >= 7:
        return "heavy underdog: pass-leaning, WR/TE volume up, RB carries down"
    if spread >= 3:
        return "underdog: mild pass lean"
    return "close game: no strong script"


class ContextSource(Source):
    name = "context"

    # -- betting market ------------------------------------------------------

    def fetch_odds(self, force: bool = False) -> list[dict[str, Any]]:
        api_key = env("ODDS_API_KEY")
        if not api_key:
            log.info("ODDS_API_KEY not set; skipping betting market context.")
            return []
        result = self.get_json(
            ODDS_BASE,
            "context:odds:nfl",
            params={
                "apiKey": api_key,
                "regions": "us",
                "markets": "spreads,totals",
                "oddsFormat": "american",
            },
            max_age_hours=6,
            force=force,
        )
        return result.payload if isinstance(result.payload, list) else []

    def sync_odds(self, season: int, week: int, force: bool = False) -> int:
        """Store spread, total and implied total per team for the week."""
        games = self.fetch_odds(force)
        if not games:
            return 0

        fetched_at = db.utcnow()
        stored = 0
        for game in games:
            home = normalize_team(_abbr(game.get("home_team")))
            away = normalize_team(_abbr(game.get("away_team")))
            if not (home and away):
                continue

            spreads, total = _consensus(game)
            for team, opponent, is_home in ((home, away, 1), (away, home, 0)):
                spread = spreads.get(team)
                self.conn.execute(
                    "INSERT INTO game_context(season, week, team, opponent, is_home, "
                    "spread, total, implied_total, fetched_at) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(season, week, team) DO UPDATE SET "
                    "opponent=excluded.opponent, is_home=excluded.is_home, "
                    "spread=excluded.spread, total=excluded.total, "
                    "implied_total=excluded.implied_total, fetched_at=excluded.fetched_at",
                    (
                        season, week, team, opponent, is_home, spread, total,
                        implied_total(total, spread), fetched_at,
                    ),
                )
                stored += 1
        self.conn.commit()
        return stored

    # -- weather -------------------------------------------------------------

    def sync_weather(self, season: int, week: int, force: bool = False) -> int:
        """Wind, temperature and precipitation at each outdoor venue."""
        rows = self.conn.fetchall(
            "SELECT team, is_home FROM game_context WHERE season=? AND week=?",
            (season, week),
        )
        hosts = [r["team"] for r in rows if r["is_home"]]
        if not hosts:
            return 0

        updated = 0
        for team in hosts:
            venue = STADIUMS.get(team)
            if not venue:
                continue
            latitude, longitude, is_dome = venue

            wind = temp = precip = None
            if not is_dome:
                try:
                    result = self.get_json(
                        WEATHER_BASE,
                        f"context:weather:{team}:{season}:{week}",
                        params={
                            "latitude": latitude,
                            "longitude": longitude,
                            "daily": "wind_speed_10m_max,temperature_2m_max,"
                                     "precipitation_probability_max",
                            "temperature_unit": "fahrenheit",
                            "wind_speed_unit": "mph",
                            "forecast_days": 7,
                        },
                        max_age_hours=6,
                        force=force,
                    )
                    daily = (result.payload or {}).get("daily", {})
                    wind = _first(daily.get("wind_speed_10m_max"))
                    temp = _first(daily.get("temperature_2m_max"))
                    precip = _first(daily.get("precipitation_probability_max"))
                except Exception as exc:
                    log.info("weather unavailable for %s: %s", team, exc)

            # Both teams in a game share the venue's conditions.
            self.conn.execute(
                "UPDATE game_context SET wind_mph=?, temp_f=?, precip_pct=?, is_dome=? "
                "WHERE season=? AND week=? AND (team=? OR opponent=?)",
                (wind, temp, precip, 1 if is_dome else 0, season, week, team, team),
            )
            updated += 1
        self.conn.commit()
        return updated

    # -- reading it back -----------------------------------------------------

    def for_team(self, season: int, week: int, team: str) -> dict[str, Any] | None:
        row = self.conn.fetchone(
            "SELECT * FROM game_context WHERE season=? AND week=? AND team=?",
            (season, week, normalize_team(team)),
        )
        if not row:
            return None
        out = dict(row)
        out["script"] = game_script(out.get("spread"))
        out["windy"] = bool(
            not out.get("is_dome")
            and (out.get("wind_mph") or 0) >= WIND_CONCERN_MPH
        )
        return out

    def notes_for(self, season: int, week: int, team: str) -> list[str]:
        """Short, human-readable context lines for a team's week."""
        context = self.for_team(season, week, team)
        if not context:
            return []
        notes = []
        if context.get("implied_total") is not None:
            notes.append(
                f"implied for {context['implied_total']:.1f} pts "
                f"vs {context.get('opponent') or '?'}"
            )
        script = context.get("script")
        if script and script != "neutral" and "no strong" not in script:
            notes.append(script)
        if context.get("windy"):
            notes.append(
                f"wind {context['wind_mph']:.0f} mph — passing and kicking suppressed"
            )
        return notes

    def health(self) -> dict[str, Any]:
        has_key = bool(env("ODDS_API_KEY"))
        n = self.conn.scalar("SELECT COUNT(*) FROM game_context") or 0
        return {
            "source": self.name,
            "ok": True,
            "odds_key": "set" if has_key else "not set (weather only)",
            "rows": n,
        }


def _abbr(name: str | None) -> str:
    """The odds feed uses full team names; map to the abbreviation."""
    if not name:
        return ""
    words = str(name).split()
    city = " ".join(words[:-1]).upper()
    known = {
        "ARIZONA": "ARI", "ATLANTA": "ATL", "BALTIMORE": "BAL", "BUFFALO": "BUF",
        "CAROLINA": "CAR", "CHICAGO": "CHI", "CINCINNATI": "CIN", "CLEVELAND": "CLE",
        "DALLAS": "DAL", "DENVER": "DEN", "DETROIT": "DET", "GREEN BAY": "GB",
        "HOUSTON": "HOU", "INDIANAPOLIS": "IND", "JACKSONVILLE": "JAX",
        "KANSAS CITY": "KC", "LAS VEGAS": "LV", "LOS ANGELES CHARGERS": "LAC",
        "LOS ANGELES RAMS": "LAR", "MIAMI": "MIA", "MINNESOTA": "MIN",
        "NEW ENGLAND": "NE", "NEW ORLEANS": "NO", "NEW YORK GIANTS": "NYG",
        "NEW YORK JETS": "NYJ", "PHILADELPHIA": "PHI", "PITTSBURGH": "PIT",
        "SAN FRANCISCO": "SF", "SEATTLE": "SEA", "TAMPA BAY": "TB",
        "TENNESSEE": "TEN", "WASHINGTON": "WAS",
    }
    full = str(name).upper()
    for label, abbr in known.items():
        if full.startswith(label):
            return abbr
    return known.get(city, "")


def _consensus(game: dict[str, Any]) -> tuple[dict[str, float], float | None]:
    """Median spread per team and median game total across books.

    Median rather than a single book: one outlier line should not move the
    projection, and books disagree by half a point routinely.
    """
    spreads: dict[str, list[float]] = {}
    totals: list[float] = []

    for book in game.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            key = market.get("key")
            for outcome in market.get("outcomes", []) or []:
                if key == "spreads":
                    team = normalize_team(_abbr(outcome.get("name")))
                    point = outcome.get("point")
                    if team and point is not None:
                        spreads.setdefault(team, []).append(float(point))
                elif key == "totals" and outcome.get("point") is not None:
                    totals.append(float(outcome["point"]))

    def median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    return (
        {team: median(v) for team, v in spreads.items() if median(v) is not None},
        median(totals),
    )


def _first(values: Any) -> float | None:
    try:
        return float(values[0]) if values else None
    except (TypeError, ValueError, IndexError):
        return None
