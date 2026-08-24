"""Sleeper adapter: player database, injuries, and trending adds/drops.

Free, unauthenticated. Verified against the live API (docs.sleeper.com):
  GET /v1/players/nfl                        - full player dump (~14MB)
  GET /v1/players/nfl/trending/{add|drop}    - trending across all Sleeper leagues
  GET /v1/state/nfl                          - current season / week

Two things make this source valuable beyond injuries:

1. Sleeper records cross-reference ids (`yahoo_id`, `espn_id`, `gsis_id`), so it
   doubles as the Rosetta Stone for id mapping - an exact bridge between Yahoo
   and nflverse instead of fuzzy name matching (spec 7).
2. Trending adds are a leading indicator of waiver heat, visible before the
   managers in our league react (spec 3.2, 6.1).

Note: Sleeper exposes `injury_status` and `injury_body_part`, but NOT practice
participation - that field is absent from every record in the live dump. Practice
designations therefore come from the NFL injury report adapter, not from here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from src import db
from src.idmap import IdMapper, normalize_position, normalize_team
from src.sources.base import Source

BASE = "https://api.sleeper.app/v1"

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

# Sleeper injury_status values seen in the live feed, normalized to our vocabulary.
INJURY_STATUS_MAP = {
    "Out": "Out",
    "Doubtful": "Doubtful",
    "Questionable": "Questionable",
    "IR": "IR",
    "PUP": "PUP",
    "NA": "NA",
    "DNR": "DNR",
    "Sus": "Suspended",
    "COV": "COVID",
}

#: Severity ordering, used to decide whether a status change is an escalation.
SEVERITY = {
    "Healthy": 0, "DNR": 1, "Questionable": 2, "Doubtful": 3,
    "Out": 4, "Suspended": 4, "NA": 4, "COVID": 4, "PUP": 5, "IR": 6,
}


@dataclass
class SleeperPlayer:
    sleeper_id: str
    full_name: str
    position: str
    team: str
    yahoo_id: str | None
    espn_id: str | None
    gsis_id: str | None
    injury_status: str | None
    injury_body_part: str | None
    injury_notes: str | None
    depth_chart_order: int | None
    depth_chart_position: str | None
    search_rank: int | None
    years_exp: int | None
    active: bool


class SleeperSource(Source):
    name = "sleeper"
    #: Sleeper explicitly asks that the ~14MB dump be pulled at most once a day.
    PLAYER_DUMP_MAX_AGE_HOURS = 20

    def __init__(self, conn: sqlite3.Connection, **kwargs):
        super().__init__(conn, **kwargs)
        self.request_delay_seconds = 0.2  # far under the 1000/min limit

    # -- state ---------------------------------------------------------------

    def state(self, force: bool = False) -> dict[str, Any]:
        """Current season, week and season type (pre/regular/post)."""
        result = self.get_json(
            f"{BASE}/state/nfl", "sleeper:state", max_age_hours=1, force=force
        )
        return result.payload or {}

    def current_week(self) -> int:
        return int(self.state().get("week") or 1)

    def current_season(self) -> int:
        return int(self.state().get("season") or 0)

    def is_regular_season(self) -> bool:
        return str(self.state().get("season_type", "")).lower() in ("regular", "post")

    # -- player dump ---------------------------------------------------------

    def players(self, force: bool = False) -> dict[str, dict[str, Any]]:
        result = self.get_json(
            f"{BASE}/players/nfl",
            "sleeper:players:nfl",
            max_age_hours=self.PLAYER_DUMP_MAX_AGE_HOURS,
            timeout=90,
            force=force,
        )
        return result.payload or {}

    def fantasy_players(self, force: bool = False) -> list[SleeperPlayer]:
        """Active players at fantasy-relevant positions."""
        out: list[SleeperPlayer] = []
        for pid, raw in self.players(force=force).items():
            if not isinstance(raw, dict):
                continue
            position = normalize_position(raw.get("position"))
            if position not in FANTASY_POSITIONS:
                continue
            if not raw.get("active", False):
                continue
            name = raw.get("full_name") or " ".join(
                filter(None, [raw.get("first_name"), raw.get("last_name")])
            )
            if not name:
                continue
            out.append(
                SleeperPlayer(
                    sleeper_id=str(pid),
                    full_name=name,
                    position=position,
                    team=normalize_team(raw.get("team")),
                    yahoo_id=_str_or_none(raw.get("yahoo_id")),
                    espn_id=_str_or_none(raw.get("espn_id")),
                    gsis_id=_str_or_none(raw.get("gsis_id")),
                    injury_status=INJURY_STATUS_MAP.get(
                        str(raw.get("injury_status") or "").strip()
                    ),
                    injury_body_part=raw.get("injury_body_part"),
                    injury_notes=raw.get("injury_notes"),
                    depth_chart_order=_int_or_none(raw.get("depth_chart_order")),
                    depth_chart_position=raw.get("depth_chart_position"),
                    search_rank=_int_or_none(raw.get("search_rank")),
                    years_exp=_int_or_none(raw.get("years_exp")),
                    active=bool(raw.get("active")),
                )
            )
        return out

    # -- persistence ---------------------------------------------------------

    def sync_players(self, idmap: IdMapper, force: bool = False) -> dict[str, int]:
        """Register Sleeper players and their cross-reference ids.

        Because Sleeper carries `yahoo_id`, this is what lets a Yahoo player and
        an nflverse stat line resolve to the same canonical key without guessing.
        """
        stats = {"seen": 0, "linked_yahoo": 0, "linked_gsis": 0, "matched": 0, "new": 0}
        for p in self.fantasy_players(force=force):
            stats["seen"] += 1

            # Prefer linking onto an existing canonical player via yahoo_id.
            existing_key = None
            if p.yahoo_id:
                row = self.conn.execute(
                    "SELECT player_key FROM players WHERE yahoo_id=?", (p.yahoo_id,)
                ).fetchone()
                if row:
                    existing_key = row["player_key"]
                    stats["matched"] += 1

            key = idmap.upsert_player(
                full_name=p.full_name,
                position=p.position,
                team=p.team,
                sleeper_id=p.sleeper_id,
                yahoo_id=p.yahoo_id,
                espn_id=p.espn_id,
                gsis_id=p.gsis_id,
            )
            if p.yahoo_id:
                stats["linked_yahoo"] += 1
            if p.gsis_id:
                stats["linked_gsis"] += 1
            if existing_key is None:
                stats["new"] += 1

            # Keep the Sleeper id resolvable even when names disagree later.
            idmap.record_mapping("sleeper", p.sleeper_id, key, "exact", 1.0)
        self.conn.commit()
        return stats

    def sync_injuries(self, idmap: IdMapper, force: bool = False) -> int:
        """Record the current injury state of every player carrying one."""
        observed_at = db.utcnow()
        count = 0
        for p in self.fantasy_players(force=force):
            if not p.injury_status:
                continue
            result = idmap.resolve(
                source="sleeper", source_id=p.sleeper_id, name=p.full_name,
                position=p.position, team=p.team,
            )
            if not result.player_key:
                continue
            self.conn.execute(
                "INSERT INTO injuries(player_key, status, practice, body_part, note, source, "
                "observed_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(player_key, source, observed_at) DO UPDATE SET "
                "status=excluded.status, body_part=excluded.body_part, note=excluded.note",
                (
                    result.player_key, p.injury_status, None, p.injury_body_part,
                    p.injury_notes, self.name, observed_at,
                ),
            )
            count += 1
        self.conn.commit()
        return count

    # -- trending ------------------------------------------------------------

    def trending(
        self, kind: str = "add", lookback_hours: int = 24, limit: int = 50,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        if kind not in ("add", "drop"):
            raise ValueError("kind must be 'add' or 'drop'")
        result = self.get_json(
            f"{BASE}/players/nfl/trending/{kind}",
            f"sleeper:trending:{kind}:{lookback_hours}:{limit}",
            params={"lookback_hours": lookback_hours, "limit": limit},
            max_age_hours=1,
            force=force,
        )
        return result.payload or []

    def sync_trending(
        self, idmap: IdMapper, lookback_hours: int = 24, limit: int = 50,
    ) -> dict[str, int]:
        counts = {"add": 0, "drop": 0}
        players = self.players()
        fetched_at = db.utcnow()
        for kind in ("add", "drop"):
            for entry in self.trending(kind, lookback_hours, limit):
                sid = str(entry.get("player_id") or "")
                raw = players.get(sid) or {}
                name = raw.get("full_name") or ""
                if not name:
                    continue
                result = idmap.resolve(
                    source="sleeper", source_id=sid, name=name,
                    position=raw.get("position"), team=raw.get("team"),
                )
                if not result.player_key:
                    continue
                self.conn.execute(
                    "INSERT INTO trending(player_key, kind, count, lookback_hours, fetched_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(player_key, kind, fetched_at) "
                    "DO UPDATE SET count=excluded.count",
                    (result.player_key, kind, _int_or_none(entry.get("count")),
                     lookback_hours, fetched_at),
                )
                counts[kind] += 1
        self.conn.commit()
        return counts

    # -- health --------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        try:
            state = self.state()
            return {
                "source": self.name,
                "ok": bool(state),
                "season": state.get("season"),
                "week": state.get("week"),
                "season_type": state.get("season_type"),
            }
        except Exception as exc:
            return {"source": self.name, "ok": False, "error": str(exc)}


def severity_of(status: str | None) -> int:
    return SEVERITY.get(status or "Healthy", 0)


def is_escalation(before: str | None, after: str | None) -> bool:
    """True when a status change is a worsening worth alerting on (spec 6.2)."""
    return severity_of(after) > severity_of(before)


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
