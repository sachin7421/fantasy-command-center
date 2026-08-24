"""FantasyPros consensus rankings (spec 3.4).

FantasyPros has no free public API. Rather than scraping, this adapter reads the
ffverse mirror of their published rankings via `nflreadpy.load_ff_rankings()`,
which carries expert consensus rank (ECR) together with the disagreement between
experts (`sd`, `best`, `worst`) and bye weeks.

That spread is better input for the draft survival model than a bare ADP number:
it is the market's own uncertainty about where a player goes (spec 5.1).

CSV import stays first-class (spec 3.4): drop FantasyPros exports into the
configured directory and they take precedence, so a mirror outage or a custom
scoring export never blocks draft prep.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src import db
from src.idmap import IdMapper, normalize_position, normalize_team
from src.sources.base import Source

log = logging.getLogger(__name__)

#: The ffverse slice holding overall redraft consensus rankings.
OVERALL_PAGE = "redraft-overall"
SUPERFLEX_PAGE = "redraft-op"

#: Column aliases across the various FantasyPros CSV exports.
CSV_ALIASES = {
    "player": ("player", "player name", "name", "overall"),
    "pos": ("pos", "position"),
    "team": ("team", "tm"),
    "ecr": ("rk", "rank", "ecr", "avg", "avg.", "adp"),
    "sd": ("sd", "std dev", "std.dev", "stdev"),
    "best": ("best",),
    "worst": ("worst",),
    "bye": ("bye", "bye week"),
}


@dataclass
class Ranking:
    name: str
    position: str
    team: str
    ecr: float
    sd: float | None
    best: float | None
    worst: float | None
    bye: int | None


class FantasyProsSource(Source):
    name = "fantasypros"

    def __init__(self, conn: sqlite3.Connection, csv_dir: str | Path | None = None, **kwargs):
        super().__init__(conn, **kwargs)
        self.csv_dir = Path(csv_dir) if csv_dir else None

    # -- loading -------------------------------------------------------------

    def load(self, superflex: bool = False, force: bool = False) -> list[Ranking]:
        """CSV exports if present, otherwise the ffverse mirror."""
        from_csv = self._load_csv()
        if from_csv:
            log.info("FantasyPros: using %d rows from CSV export", len(from_csv))
            return from_csv
        return self._load_ffverse(superflex, force)

    def _load_ffverse(self, superflex: bool, force: bool) -> list[Ranking]:
        page = SUPERFLEX_PAGE if superflex else OVERALL_PAGE
        cache_key = f"fantasypros:ecr:{page}"

        try:
            import nflreadpy as nfl
            import polars as pl

            frame = nfl.load_ff_rankings()
            frame = frame.filter(pl.col("page_type") == page)
            rows = [
                {
                    "player": r["player"], "pos": r["pos"], "team": r["team"],
                    "ecr": r["ecr"], "sd": r["sd"], "best": r["best"],
                    "worst": r["worst"], "bye": r["bye"],
                }
                for r in frame.iter_rows(named=True)
            ]
            db.cache_put(self.conn, cache_key, self.name, rows)
        except Exception as exc:
            cached = db.cache_get(self.conn, cache_key)
            if cached is None:
                log.warning("FantasyPros unavailable and nothing cached: %s", exc)
                return []
            rows, fetched_at = cached
            log.warning("FantasyPros fetch failed (%s); using cache from %s", exc, fetched_at)

        return [
            Ranking(
                name=str(r.get("player") or ""),
                position=normalize_position(r.get("pos")),
                team=normalize_team(r.get("team")),
                ecr=float(r["ecr"]),
                sd=_float_or_none(r.get("sd")),
                best=_float_or_none(r.get("best")),
                worst=_float_or_none(r.get("worst")),
                bye=_int_or_none(r.get("bye")),
            )
            for r in rows
            if r.get("player") and r.get("ecr") is not None
        ]

    def _load_csv(self) -> list[Ranking]:
        if not self.csv_dir or not self.csv_dir.exists():
            return []
        out: list[Ranking] = []
        for path in sorted(self.csv_dir.glob("*.csv")):
            try:
                out.extend(self._parse_csv(path))
            except Exception as exc:
                log.warning("Could not parse %s: %s", path, exc)
        return out

    @staticmethod
    def _parse_csv(path: Path) -> list[Ranking]:
        rows: list[Ranking] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            headers = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}

            def pick(field: str) -> str | None:
                for alias in CSV_ALIASES[field]:
                    if alias in headers:
                        return headers[alias]
                return None

            col = {k: pick(k) for k in CSV_ALIASES}
            if not (col["player"] and col["ecr"]):
                return []

            for raw in reader:
                name = (raw.get(col["player"]) or "").strip()
                if not name:
                    continue
                ecr = _float_or_none(raw.get(col["ecr"]))
                if ecr is None:
                    continue
                position = raw.get(col["pos"]) if col["pos"] else ""
                # FantasyPros positional exports write "RB1", "WR12" etc.
                position = "".join(ch for ch in str(position) if ch.isalpha())
                rows.append(
                    Ranking(
                        name=name,
                        position=normalize_position(position),
                        team=normalize_team(raw.get(col["team"]) if col["team"] else ""),
                        ecr=ecr,
                        sd=_float_or_none(raw.get(col["sd"])) if col["sd"] else None,
                        best=_float_or_none(raw.get(col["best"])) if col["best"] else None,
                        worst=_float_or_none(raw.get(col["worst"])) if col["worst"] else None,
                        bye=_int_or_none(raw.get(col["bye"])) if col["bye"] else None,
                    )
                )
        return rows

    # -- persistence ---------------------------------------------------------

    def sync(
        self, idmap: IdMapper, superflex: bool = False, force: bool = False
    ) -> dict[str, int]:
        """Store ECR as ADP, and fill in bye weeks (which Sleeper omits)."""
        stats = {"loaded": 0, "stored": 0, "unmatched": 0, "byes": 0}
        fetched_at = db.utcnow()

        for ranking in self.load(superflex, force):
            stats["loaded"] += 1
            match = idmap.resolve(
                source=self.name, source_id=None, name=ranking.name,
                position=ranking.position, team=ranking.team,
            )
            if not match.player_key:
                stats["unmatched"] += 1
                continue

            self.conn.execute(
                "INSERT INTO adp(player_key, source, adp, stdev, best, worst, fetched_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(player_key, source, fetched_at) "
                "DO UPDATE SET adp=excluded.adp, stdev=excluded.stdev, "
                "best=excluded.best, worst=excluded.worst",
                (
                    match.player_key, self.name, ranking.ecr, ranking.sd,
                    ranking.best, ranking.worst, fetched_at,
                ),
            )
            stats["stored"] += 1

            if ranking.bye:
                self.conn.execute(
                    "UPDATE players SET bye_week=? WHERE player_key=? "
                    "AND (bye_week IS NULL OR bye_week<>?)",
                    (ranking.bye, match.player_key, ranking.bye),
                )
                stats["byes"] += 1
        self.conn.commit()
        return stats

    def health(self) -> dict[str, Any]:
        try:
            rows = self.load()
            return {"source": self.name, "ok": bool(rows), "rankings": len(rows)}
        except Exception as exc:
            return {"source": self.name, "ok": False, "error": str(exc)}


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    f = _float_or_none(value)
    return int(f) if f is not None else None
