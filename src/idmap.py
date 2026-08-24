"""Cross-source player identity.

Yahoo, Sleeper, FantasyPros and nflverse all name players slightly differently
("D.K. Metcalf" / "DK Metcalf", "Kenneth Walker III" / "Kenneth Walker").
This module normalizes names into a canonical `player_key` and resolves each
source id onto it, escalating exact -> alias -> fuzzy, with a manual override
file for the stragglers (spec 7).
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.db import utcnow

# Suffixes that sources disagree about attaching.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Team abbreviation drift between sources.
TEAM_ALIASES = {
    "JAC": "JAX", "WSH": "WAS", "LA": "LAR", "SD": "LAC", "OAK": "LV",
    "STL": "LAR", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "LVR": "LV", "NOR": "NO", "NWE": "NE", "SFO": "SF", "TAM": "TB",
    "KAN": "KC", "GNB": "GB", "FA": "", "None": "",
}

# Positions normalized to a single vocabulary.
POSITION_ALIASES = {
    "PK": "K", "DST": "DEF", "D/ST": "DEF", "DEF": "DEF", "D": "DEF",
    "FB": "RB", "HB": "RB", "WR/RB": "WR", "OL": "OL",
}

# Nickname / spelling variants that fuzzy matching alone gets wrong.
NAME_ALIASES = {
    "mitch trubisky": "mitchell trubisky",
    "gabe davis": "gabriel davis",
    "josh palmer": "joshua palmer",
    "cam ward": "cameron ward",
    "chig okonkwo": "chigoziem okonkwo",
    "tank bigsby": "thomas bigsby",
    "bucky irving": "montrell irving",
    "hollywood brown": "marquise brown",
    "scotty miller": "scott miller",
    "nick westbrook ikhine": "nicholas westbrook ikhine",
}


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize_name(name: str) -> str:
    """Fold a player name to a comparable form.

    "D.K. Metcalf" -> "dk metcalf"; "Kenneth Walker III" -> "kenneth walker".
    """
    if not name:
        return ""
    s = strip_accents(str(name)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[.'`]", "", s)          # D.K. -> DK, Ja'Marr -> JaMarr
    s = re.sub(r"[^a-z0-9 ]+", " ", s)   # hyphens/slashes become spaces
    parts = [p for p in s.split() if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    s = " ".join(parts)
    return NAME_ALIASES.get(s, s)


def normalize_team(team: str | None) -> str:
    if not team:
        return ""
    t = str(team).strip().upper()
    return TEAM_ALIASES.get(t, t)


def normalize_position(pos: str | None) -> str:
    if not pos:
        return ""
    p = str(pos).strip().upper()
    return POSITION_ALIASES.get(p, p)


def make_player_key(name: str, position: str | None, team: str | None = None) -> str:
    """Canonical id. Team is deliberately excluded: players change teams."""
    norm_pos = normalize_position(position)
    norm_name = normalize_name(name)
    if norm_pos == "DEF":
        # Team defenses are identified by team, since their "name" varies wildly
        # ("San Francisco", "49ers", "San Francisco 49ers").
        return f"DEF|{normalize_team(team) or norm_name}"
    return f"{norm_name}|{norm_pos}"


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class MatchResult:
    player_key: str | None
    method: str          # exact|alias|fuzzy|manual|unmatched
    confidence: float
    candidate_name: str | None = None


class IdMapper:
    """Resolves source-specific player records onto canonical player keys."""

    #: A fuzzy match below this is rejected rather than guessed at.
    FUZZY_THRESHOLD = 0.87

    def __init__(self, conn: sqlite3.Connection, overrides_path: str | Path | None = None):
        self.conn = conn
        self.overrides = self._load_overrides(overrides_path)
        self._index_dirty = True
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_norm_name: dict[str, list[dict[str, Any]]] = {}

    # -- overrides -----------------------------------------------------------

    @staticmethod
    def _load_overrides(path: str | Path | None) -> dict[str, dict[str, str]]:
        """Manual overrides: {source: {source_id: player_key}}."""
        if not path:
            return {}
        p = Path(path)
        if not p.exists():
            return {}
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return {str(k): dict(v or {}) for k, v in data.items()}

    # -- registry ------------------------------------------------------------

    def _refresh_index(self) -> None:
        if not self._index_dirty:
            return
        self._by_key.clear()
        self._by_norm_name.clear()
        for row in self.conn.execute(
            "SELECT player_key, full_name, position, team FROM players"
        ):
            rec = dict(row)
            self._by_key[rec["player_key"]] = rec
            self._by_norm_name.setdefault(normalize_name(rec["full_name"]), []).append(rec)
        self._index_dirty = False

    def upsert_player(
        self,
        *,
        full_name: str,
        position: str | None,
        team: str | None = None,
        bye_week: int | None = None,
        status: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        **source_ids: str | None,
    ) -> str:
        """Insert or update a canonical player row; returns the player_key.

        `source_ids` accepts yahoo_id, yahoo_key, sleeper_id, gsis_id, espn_id.
        Only non-empty values overwrite what is already stored, so a partial
        update from one source never erases another source's id.
        """
        key = make_player_key(full_name, position, team)
        allowed = {"yahoo_id", "yahoo_key", "sleeper_id", "gsis_id", "espn_id"}
        ids = {k: v for k, v in source_ids.items() if k in allowed and v}

        existing = self.conn.execute(
            "SELECT * FROM players WHERE player_key=?", (key,)
        ).fetchone()

        if existing is None:
            cols = {
                "player_key": key,
                "full_name": full_name,
                "first_name": first_name,
                "last_name": last_name,
                "position": normalize_position(position),
                "team": normalize_team(team),
                "bye_week": bye_week,
                "status": status,
                "updated_at": utcnow(),
                **ids,
            }
            placeholders = ", ".join("?" for _ in cols)
            self.conn.execute(
                f"INSERT INTO players ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(cols.values()),
            )
        else:
            updates: dict[str, Any] = {"updated_at": utcnow()}
            if team:
                updates["team"] = normalize_team(team)
            if bye_week is not None:
                updates["bye_week"] = bye_week
            if status is not None:
                updates["status"] = status
            if first_name:
                updates["first_name"] = first_name
            if last_name:
                updates["last_name"] = last_name
            updates.update(ids)
            assignments = ", ".join(f"{k}=?" for k in updates)
            self.conn.execute(
                f"UPDATE players SET {assignments} WHERE player_key=?",
                (*updates.values(), key),
            )

        for source, source_id in (
            ("yahoo", ids.get("yahoo_id")),
            ("sleeper", ids.get("sleeper_id")),
            ("nflverse", ids.get("gsis_id")),
            ("espn", ids.get("espn_id")),
        ):
            if source_id:
                self.record_mapping(source, str(source_id), key, "exact", 1.0)

        self._index_dirty = True
        return key

    def record_mapping(
        self, source: str, source_id: str, player_key: str, method: str, confidence: float
    ) -> None:
        self.conn.execute(
            "INSERT INTO player_id_map(source, source_id, player_key, method, confidence, "
            "updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source, source_id) DO UPDATE SET player_key=excluded.player_key, "
            "method=excluded.method, confidence=excluded.confidence, "
            "updated_at=excluded.updated_at",
            (source, str(source_id), player_key, method, confidence, utcnow()),
        )

    # -- resolution ----------------------------------------------------------

    def resolve(
        self,
        *,
        source: str,
        source_id: str | None = None,
        name: str = "",
        position: str | None = None,
        team: str | None = None,
        persist: bool = True,
    ) -> MatchResult:
        """Find the canonical player_key for one source record."""
        # 1. Manual override always wins.
        if source_id and str(source_id) in self.overrides.get(source, {}):
            key = self.overrides[source][str(source_id)]
            if persist:
                self.record_mapping(source, str(source_id), key, "manual", 1.0)
            return MatchResult(key, "manual", 1.0)

        # 2. Already-resolved id.
        if source_id:
            row = self.conn.execute(
                "SELECT player_key, method, confidence FROM player_id_map "
                "WHERE source=? AND source_id=?",
                (source, str(source_id)),
            ).fetchone()
            if row:
                return MatchResult(row["player_key"], row["method"], row["confidence"] or 1.0)

        self._refresh_index()

        # 3. Exact canonical key.
        key = make_player_key(name, position, team)
        if key in self._by_key:
            if persist and source_id:
                self.record_mapping(source, str(source_id), key, "exact", 1.0)
            return MatchResult(key, "exact", 1.0)

        # 4. Same normalized name, position differs or is missing.
        norm = normalize_name(name)
        candidates = self._by_norm_name.get(norm, [])
        if len(candidates) == 1:
            match = candidates[0]
            if persist and source_id:
                self.record_mapping(source, str(source_id), match["player_key"], "alias", 0.95)
            return MatchResult(match["player_key"], "alias", 0.95, match["full_name"])
        if candidates:
            wanted_team = normalize_team(team)
            same_team = [c for c in candidates if normalize_team(c["team"]) == wanted_team]
            if len(same_team) == 1:
                match = same_team[0]
                if persist and source_id:
                    self.record_mapping(
                        source, str(source_id), match["player_key"], "alias", 0.93
                    )
                return MatchResult(match["player_key"], "alias", 0.93, match["full_name"])

        # 5. Fuzzy, restricted to the same position to avoid absurd matches.
        best, best_score = None, 0.0
        wanted_pos = normalize_position(position)
        for rec in self._by_key.values():
            if wanted_pos and normalize_position(rec["position"]) != wanted_pos:
                continue
            score = similarity(norm, normalize_name(rec["full_name"]))
            if score > best_score:
                best, best_score = rec, score

        if best and best_score >= self.FUZZY_THRESHOLD:
            if persist and source_id:
                self.record_mapping(
                    source, str(source_id), best["player_key"], "fuzzy", best_score
                )
            return MatchResult(best["player_key"], "fuzzy", best_score, best["full_name"])

        return MatchResult(
            None, "unmatched", best_score, best["full_name"] if best else None
        )

    def unmatched_report(self, records: Iterable[dict[str, Any]], source: str) -> list[dict]:
        """Resolve a batch and return the failures, for the override file."""
        misses = []
        for rec in records:
            result = self.resolve(
                source=source,
                source_id=rec.get("id"),
                name=rec.get("name", ""),
                position=rec.get("position"),
                team=rec.get("team"),
                persist=False,
            )
            if result.player_key is None:
                misses.append(
                    {
                        "source": source,
                        "source_id": rec.get("id"),
                        "name": rec.get("name"),
                        "position": rec.get("position"),
                        "team": rec.get("team"),
                        "closest": result.candidate_name,
                        "score": round(result.confidence, 3),
                    }
                )
        return misses
