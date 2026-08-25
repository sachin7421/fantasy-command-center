"""Sleeper projections + ADP.

Undocumented but stable public endpoints, verified live against the 2026 season:
  GET /projections/nfl/{season}?season_type=regular&position[]=RB   - season totals
  GET /projections/nfl/{season}/{week}?season_type=regular&...      - weekly

Why this source leads the blend: it returns *raw stat lines* under readable
names (`rush_yd`, `rec_td`, `pass_int`), which is exactly what the spec requires
- we score every projection with our own league rules and never trust a source's
pre-computed points (spec 4.2). It also carries ADP in several scoring formats,
which feeds the draft survival model (spec 5.1).

Coverage is strong for QB/RB/WR/TE. Kicker and defense lines are sparser, so the
ESPN adapter is the better contributor for those positions.
"""
from __future__ import annotations

from typing import Any
from collections.abc import Iterable

from src import db
from src.idmap import IdMapper
from src.sources.base import Source
from src.storage import Database

#: Note: projections live at the API root, NOT under the documented /v1 prefix.
PROJECTIONS_BASE = "https://api.sleeper.app"

#: Sleeper stat name -> our canonical stat vocabulary (see src/scoring.py).
STAT_MAP: dict[str, str] = {
    # Passing
    "pass_att": "pass_att", "pass_cmp": "pass_cmp", "pass_yd": "pass_yds",
    "pass_td": "pass_td", "pass_int": "pass_int",
    # Yahoo has ONE 2-point category; all three flavours map onto it.
    "pass_2pt": "two_pt",
    "pass_fd": "pass_fd", "pass_sack": "pass_sacked",
    # Rushing
    "rush_att": "rush_att", "rush_yd": "rush_yds", "rush_td": "rush_td",
    "rush_2pt": "two_pt", "rush_fd": "rush_fd",
    # Receiving
    "rec": "rec", "rec_yd": "rec_yds", "rec_td": "rec_td",
    "rec_2pt": "two_pt", "rec_tgt": "rec_tgt", "rec_fd": "rec_fd",
    # Turnovers
    "fum": "fum", "fum_lost": "fum_lost",
    # Kicking
    "fgm_0_19": "fg_0_19", "fgm_20_29": "fg_20_29", "fgm_30_39": "fg_30_39",
    "fgm_40_49": "fg_40_49", "fgm_50p": "fg_50p",
    "xpm": "pat_made", "xpmiss": "pat_miss",
    # Defense / special teams
    "sack": "def_sack", "int": "def_int", "fum_rec": "def_fum_rec",
    "blk_kick": "def_blk_kick", "safe": "def_safety",
    "pts_allow": "def_pts_allowed", "yds_allow": "def_yds_allowed",
}

# Defensive touchdowns need care: Sleeper reports `def_td` as the TOTAL of its
# own components (JAX week 1: def_td 0.21 = def_fum_td 0.07 + pass_int_td 0.14),
# so summing them all double-counts. Same for return touchdowns, where `st_td`,
# `pr_td` and `def_pr_td` are three names for one event.
DEF_TD_TOTAL = "def_td"
DEF_TD_COMPONENTS = ("def_fum_td", "def_int_td", "pass_int_td")
RETURN_TD_TOTAL = "st_td"
RETURN_TD_COMPONENTS = (("pr_td", "def_pr_td"), ("kr_td", "def_kr_td"))

#: Field-goal misses arrive split by distance; the league scores them as one bucket.
FG_MISS_PARTS = ("fgmiss_0_19", "fgmiss_20_29", "fgmiss_30_39", "fgmiss_40_49", "fgmiss_50p")

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def translate_stats(raw: dict[str, Any]) -> dict[str, float]:
    """Convert one Sleeper stat line into canonical stat names.

    Anything Sleeper reports that we have no canonical name for is dropped: it
    is either a scoring-format convenience (`pts_ppr`), an ADP field, or a stat
    no Yahoo league scores.
    """
    out: dict[str, float] = {}
    for key, value in (raw or {}).items():
        if value is None:
            continue
        canonical = STAT_MAP.get(key)
        if canonical:
            out[canonical] = out.get(canonical, 0.0) + float(value)

    # Prefer the reported total; fall back to components only when it is absent
    # (season lines omit `def_td` but do carry `def_fum_td`).
    def_td = _float(raw.get(DEF_TD_TOTAL))
    if def_td is None:
        def_td = sum(_float(raw.get(k)) or 0.0 for k in DEF_TD_COMPONENTS)
    if def_td:
        out["def_td"] = def_td

    return_td = _float(raw.get(RETURN_TD_TOTAL))
    if return_td is None:
        # Within each group the names are aliases, so take one, not the sum.
        return_td = sum(
            next((v for v in (_float(raw.get(k)) for k in group) if v), 0.0)
            for group in RETURN_TD_COMPONENTS
        )
    if return_td:
        out["def_ret_td"] = return_td

    fg_miss = sum(float(raw.get(k) or 0) for k in FG_MISS_PARTS)
    if fg_miss:
        out["fg_miss"] = out.get("fg_miss", 0.0) + fg_miss

    if raw.get("gp"):
        out["games"] = float(raw["gp"])
    return out


def pick_adp_field(ppr_value: float, superflex: bool = False) -> str:
    """Choose the ADP variant that matches how this league actually scores."""
    if superflex:
        return "adp_2qb"
    if ppr_value >= 0.75:
        return "adp_ppr"
    if ppr_value >= 0.25:
        return "adp_half_ppr"
    return "adp_std"


class SleeperProjections(Source):
    name = "sleeper_proj"

    def __init__(self, conn: Database, **kwargs):
        super().__init__(conn, **kwargs)
        self.request_delay_seconds = 0.2

    # -- fetching ------------------------------------------------------------

    def _fetch_position(
        self, season: int, position: str, week: int | None, force: bool
    ) -> list[dict[str, Any]]:
        path = f"{PROJECTIONS_BASE}/projections/nfl/{season}"
        if week:
            path += f"/{week}"
        cache_key = f"sleeper_proj:{season}:{week or 'season'}:{position}"
        result = self.get_json(
            path,
            cache_key,
            params={
                "season_type": "regular",
                "position[]": position,
                "order_by": "pts_half_ppr",
            },
            max_age_hours=6 if week else 24,
            force=force,
            timeout=45,
        )
        payload = result.payload
        return payload if isinstance(payload, list) else []

    def fetch(
        self,
        season: int,
        week: int | None = None,
        positions: Iterable[str] = POSITIONS,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Raw projection entries across the requested positions."""
        entries: list[dict[str, Any]] = []
        for position in positions:
            entries.extend(self._fetch_position(season, position, week, force))
        return entries

    # -- persistence ---------------------------------------------------------

    def sync(
        self,
        idmap: IdMapper,
        scoring,
        season: int,
        week: int | None = None,
        positions: Iterable[str] = POSITIONS,
        force: bool = False,
    ) -> dict[str, int]:
        """Store league-scored projections for every player we can resolve."""
        stats = {"fetched": 0, "stored": 0, "unmatched": 0}
        week_key = week or 0
        fetched_at = db.utcnow()

        for entry in self.fetch(season, week, positions, force):
            stats["fetched"] += 1
            player = entry.get("player") or {}
            sleeper_id = str(entry.get("player_id") or "")
            name = " ".join(
                filter(None, [player.get("first_name"), player.get("last_name")])
            ).strip()
            position = (player.get("fantasy_positions") or [player.get("position")])[0]
            team = entry.get("team") or player.get("team")

            if not name and position == "DEF":
                name = team or ""
            if not name:
                continue

            match = idmap.resolve(
                source="sleeper", source_id=sleeper_id, name=name,
                position=position, team=team,
            )
            if not match.player_key:
                stats["unmatched"] += 1
                continue

            stat_line = translate_stats(entry.get("stats") or {})
            if not stat_line:
                continue
            points = scoring.score(stat_line)

            self.conn.execute(
                "INSERT INTO projections(player_key, source, season, week, stats_json, "
                "points, fetched_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(player_key, source, season, week) DO UPDATE SET "
                "stats_json=excluded.stats_json, points=excluded.points, "
                "fetched_at=excluded.fetched_at",
                (
                    match.player_key, "sleeper", season, week_key,
                    _json(stat_line), points, fetched_at,
                ),
            )
            # Append-only copy, so projection drift across the season survives.
            db.record_projection_history(
                self.conn, match.player_key, "sleeper", season, week_key,
                points, _json(stat_line), fetched_at,
            )
            stats["stored"] += 1
        self.conn.commit()
        return stats

    def sync_defense_season(
        self,
        idmap: IdMapper,
        scoring,
        season: int,
        weeks: int = 18,
        force: bool = False,
    ) -> dict[str, int]:
        """Rebuild DEF season projections by summing the weekly lines.

        Sleeper's DEF *season* line is degenerate - it reports `gp: 1.0` and a
        lone `pts_allow_0: 1.0`, so points allowed contributes nothing and a
        defense is valued on sacks and takeaways alone. The weekly lines are
        well formed (`pts_allow: 16.5`), and points allowed is the single
        largest component of DST scoring, so we sum weeks instead.

        Scoring is applied per week and then totalled, which is also the only
        correct way to handle the points-allowed brackets: they are a per-game
        award, and bucketing a season total would be meaningless.
        """
        totals: dict[str, dict[str, float]] = {}
        points: dict[str, float] = {}
        counted: dict[str, int] = {}

        for week in range(1, weeks + 1):
            for entry in self._fetch_position(season, "DEF", week, force):
                team = entry.get("team") or (entry.get("player") or {}).get("team")
                if not team:
                    continue
                match = idmap.resolve(
                    source="sleeper", source_id=str(entry.get("player_id") or ""),
                    name=team, position="DEF", team=team,
                )
                if not match.player_key:
                    continue
                line = translate_stats(entry.get("stats") or {})
                if not line:
                    continue
                points[match.player_key] = points.get(match.player_key, 0.0) + scoring.score(line)
                counted[match.player_key] = counted.get(match.player_key, 0) + 1
                bucket = totals.setdefault(match.player_key, {})
                for key, value in line.items():
                    bucket[key] = bucket.get(key, 0.0) + value

        fetched_at = db.utcnow()
        stored = 0
        for player_key, total_points in points.items():
            stats_line = totals.get(player_key, {})
            stats_line["_weeks_summed"] = counted.get(player_key, 0)
            self.conn.execute(
                "INSERT INTO projections(player_key, source, season, week, stats_json, "
                "points, fetched_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(player_key, source, season, week) DO UPDATE SET "
                "stats_json=excluded.stats_json, points=excluded.points, "
                "fetched_at=excluded.fetched_at",
                (player_key, "sleeper", season, 0, _json(stats_line),
                 round(total_points, 2), fetched_at),
            )
            stored += 1
        self.conn.commit()
        return {"defenses": stored, "weeks": weeks}

    def sync_adp(
        self, idmap: IdMapper, season: int, ppr_value: float = 0.5,
        superflex: bool = False, force: bool = False,
    ) -> int:
        """Store ADP using the variant matching this league's scoring."""
        field = pick_adp_field(ppr_value, superflex)
        fetched_at = db.utcnow()
        stored = 0

        for entry in self.fetch(season, None, POSITIONS, force):
            raw = entry.get("stats") or {}
            adp = raw.get(field)
            # 999 is Sleeper's sentinel for "not ranked".
            if adp is None or float(adp) >= 900:
                continue
            player = entry.get("player") or {}
            name = " ".join(
                filter(None, [player.get("first_name"), player.get("last_name")])
            ).strip() or (entry.get("team") or "")
            match = idmap.resolve(
                source="sleeper", source_id=str(entry.get("player_id") or ""),
                name=name,
                position=(player.get("fantasy_positions") or [player.get("position")])[0],
                team=entry.get("team") or player.get("team"),
            )
            if not match.player_key:
                continue

            # Spread across the scoring variants approximates ADP disagreement,
            # which the survival model uses as sigma (spec 5.1).
            variants = [
                float(raw[k]) for k in ("adp_std", "adp_half_ppr", "adp_ppr", "adp_2qb")
                if raw.get(k) is not None and float(raw[k]) < 900
            ]
            stdev = _stdev(variants) if len(variants) > 1 else None

            self.conn.execute(
                "INSERT INTO adp(player_key, source, adp, stdev, best, worst, fetched_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(player_key, source, fetched_at) "
                "DO UPDATE SET adp=excluded.adp, stdev=excluded.stdev, "
                "best=excluded.best, worst=excluded.worst",
                (
                    match.player_key, "sleeper", float(adp), stdev,
                    min(variants) if variants else None,
                    max(variants) if variants else None,
                    fetched_at,
                ),
            )
            stored += 1
        self.conn.commit()
        return stored

    def health(self) -> dict[str, Any]:
        try:
            sample = self._fetch_position(2026, "RB", None, force=False)
            return {"source": self.name, "ok": bool(sample), "sample_size": len(sample)}
        except Exception as exc:
            return {"source": self.name, "ok": False, "error": str(exc)}


def _json(data: Any) -> str:
    import json

    return json.dumps(data)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _stdev(values: list[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5
