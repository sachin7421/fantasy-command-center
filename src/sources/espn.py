"""ESPN projections - the second opinion in the blend (spec 3.5, 4.3).

Public, unauthenticated endpoint used by ESPN's own fantasy front end:

    GET lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}
        /segments/0/leaguedefaults/3?view=kona_player_info
    header: X-Fantasy-Filter: {"players": {...}}

A second source matters for more than accuracy: with one source the
uncertainty band collapses to positional volatility alone, and floor/ceiling
stop reflecting genuine disagreement about a player (spec 4.4).

ESPN returns raw stat lines keyed by numeric ids, which we translate to our
canonical vocabulary and then score with OUR league rules - never using ESPN's
own `appliedTotal` (spec 4.2).

The stat ids below were verified against live 2026 data rather than taken from
memory; the kicker ids were checked arithmetically (74 + 77 + 80 equals the
reported made-field-goals total, id 83).
"""
from __future__ import annotations

import json
from typing import Any

from src import db
from src.idmap import IdMapper
from src.sources.base import Source
from src.storage import Database

BASE = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3"
)

#: statSourceId 1 == projection (0 == actual results).
PROJECTION_SOURCE_ID = 1
#: statSplitTypeId 0 == season total, 1 == a single week.
SEASON_SPLIT = 0
WEEK_SPLIT = 1

#: ESPN numeric stat id -> our canonical stat name.
STAT_MAP: dict[str, str] = {
    # Passing
    "0": "pass_att", "1": "pass_cmp", "2": "pass_inc", "3": "pass_yds",
    "4": "pass_td", "19": "two_pt", "20": "pass_int",
    # Rushing
    "23": "rush_att", "24": "rush_yds", "25": "rush_td", "26": "two_pt",
    # Receiving
    "42": "rec_yds", "43": "rec_td", "44": "two_pt", "53": "rec", "58": "rec_tgt",
    # Turnovers
    "68": "fum", "72": "fum_lost",
    # Kicking. ESPN buckets made field goals as 0-39 / 40-49 / 50+, which is
    # coarser than Yahoo's five buckets; the 0-39 group is split below.
    "74": "fg_50p", "77": "fg_40_49", "86": "pat_made", "88": "pat_miss",
}

#: ESPN reports one combined 0-39 bucket; Yahoo scores 0-19/20-29/30-39.
FG_SHORT_ID = "80"
FG_SHORT_SPLIT = {"fg_0_19": 0.06, "fg_20_29": 0.40, "fg_30_39": 0.54}

#: ESPN position id -> position. Used only to sanity-check id resolution.
POSITION_IDS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# Defenses are deliberately NOT contributed by this adapter. ESPN expresses DST
# points-allowed as per-game bucket COUNTS (ids 129-136, which sum to games
# played), a different shape from the scalar our scoring engine buckets on.
# Defenses are already built correctly by summing Sleeper weekly lines
# (see sleeper_projections.sync_defense_season), so mixing in a differently
# shaped source here would add error, not accuracy.
SKIP_POSITION_IDS = {16}


def translate_stats(raw: dict[str, Any]) -> dict[str, float]:
    """Convert an ESPN numeric stat line into canonical stat names."""
    out: dict[str, float] = {}
    for stat_id, value in (raw or {}).items():
        if value is None:
            continue
        canonical = STAT_MAP.get(str(stat_id))
        if canonical:
            out[canonical] = out.get(canonical, 0.0) + float(value)

    short_fgs = raw.get(FG_SHORT_ID)
    if short_fgs:
        for key, share in FG_SHORT_SPLIT.items():
            out[key] = out.get(key, 0.0) + float(short_fgs) * share
        out["_fg_short_estimated"] = 1.0
    return out


class EspnSource(Source):
    name = "espn"

    def __init__(self, conn: Database, **kwargs):
        super().__init__(conn, **kwargs)
        self.request_delay_seconds = 0.5

    # -- fetching ------------------------------------------------------------

    def fetch_players(
        self, season: int, limit: int = 500, force: bool = False
    ) -> list[dict[str, Any]]:
        """Most-rostered players, with their full projection stat lines."""
        fantasy_filter = {
            "players": {
                "limit": limit,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            }
        }
        result = self.get_json(
            BASE.format(season=season),
            f"espn:players:{season}:{limit}",
            params={"view": "kona_player_info"},
            headers={"X-Fantasy-Filter": json.dumps(fantasy_filter)},
            max_age_hours=12,
            timeout=60,
            force=force,
        )
        payload = result.payload or {}
        return payload.get("players", []) if isinstance(payload, dict) else []

    # -- persistence ---------------------------------------------------------

    def sync(
        self,
        idmap: IdMapper,
        scoring,
        season: int,
        week: int | None = None,
        limit: int = 500,
        force: bool = False,
    ) -> dict[str, int]:
        """Store ESPN projections, scored with our league rules."""
        stats = {"fetched": 0, "stored": 0, "unmatched": 0, "skipped": 0}
        want_split = WEEK_SPLIT if week else SEASON_SPLIT
        week_key = week or 0
        fetched_at = db.utcnow()

        for entry in self.fetch_players(season, limit, force):
            player = entry.get("player") or entry
            stats["fetched"] += 1

            position_id = player.get("defaultPositionId")
            if position_id in SKIP_POSITION_IDS:
                stats["skipped"] += 1
                continue

            name = player.get("fullName")
            if not name:
                continue

            candidates = [
                s for s in (player.get("stats") or [])
                if s.get("statSourceId") == PROJECTION_SOURCE_ID
                and s.get("statSplitTypeId") == want_split
                and int(s.get("seasonId") or 0) == season
            ]
            if week:
                candidates = [c for c in candidates if int(c.get("scoringPeriodId") or 0) == week]
            if not candidates:
                continue

            stat_line = translate_stats(candidates[0].get("stats") or {})
            if not stat_line:
                continue

            match = idmap.resolve(
                source=self.name,
                source_id=str(player.get("id") or ""),
                name=name,
                position=POSITION_IDS.get(position_id),
                team=None,   # ESPN uses numeric team ids; the name+pos match suffices
            )
            if not match.player_key:
                stats["unmatched"] += 1
                continue

            points = scoring.score(stat_line)
            self.conn.execute(
                "INSERT INTO projections(player_key, source, season, week, stats_json, "
                "points, fetched_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(player_key, source, season, week) DO UPDATE SET "
                "stats_json=excluded.stats_json, points=excluded.points, "
                "fetched_at=excluded.fetched_at",
                (
                    match.player_key, self.name, season, week_key,
                    json.dumps(stat_line), points, fetched_at,
                ),
            )
            db.record_projection_history(
                self.conn, match.player_key, self.name, season, week_key,
                points, json.dumps(stat_line), fetched_at,
            )
            stats["stored"] += 1
        self.conn.commit()
        return stats

    def health(self) -> dict[str, Any]:
        try:
            players = self.fetch_players(2026, limit=2)
            return {"source": self.name, "ok": bool(players), "sample": len(players)}
        except Exception as exc:
            return {"source": self.name, "ok": False, "error": str(exc)}
