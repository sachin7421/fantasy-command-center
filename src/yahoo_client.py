"""Yahoo Fantasy Sports client.

Thin wrapper over yfpy that (a) handles auth/token lifecycle, (b) serializes
yfpy model objects into plain dicts, and (c) persists everything into SQLite so
that jobs can run off cache when Yahoo is slow or down (spec 3, design rule).

Yahoo is the source of truth for the league itself: settings, scoring, roster
slots, rosters, draft results, transactions and free agents.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from collections.abc import Iterable

from src import db
from src.config import Config
from src.idmap import IdMapper
from src.scoring import LeagueScoring, build_from_yahoo
from src.storage import Database

log = logging.getLogger(__name__)

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# Roster slots that are not real starting positions.
BENCH_SLOTS = {"BN", "IR", "IR+", "NA"}

# Yahoo flex slot names -> the positions they accept.
FLEX_SLOTS = {
    "W/R": {"WR", "RB"},
    "W/T": {"WR", "TE"},
    "W/R/T": {"WR", "RB", "TE"},
    "Q/W/R/T": {"QB", "WR", "RB", "TE"},
    "W/R/T/Q": {"QB", "WR", "RB", "TE"},
    "D": {"DEF"},
}


def serialize(obj: Any) -> Any:
    """Convert a yfpy model (or nested structure of them) into plain data."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    # yfpy declares Team.name as `bytes` (models.py), and Yahoo team names are
    # user-entered, so they are not all ASCII. Without this branch a name fell
    # through every check below to the final `str(obj)` and was stored as the
    # literal text "b'Butt Fumblers'" - prefix, quotes and all.
    #
    # `errors="replace"` because one team with an undecodable name should cost
    # that name, not the entire league sync.
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(v) for v in obj]
    for method in ("clean_data_dict", "serialized"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                return serialize(fn())
            # silent: yfpy exposes several serialisers and not all work on every
            # object; trying the next one is the entire point of the loop
            except Exception:  # pragma: no cover
                continue
    if hasattr(obj, "__dict__"):
        return {
            k: serialize(v) for k, v in vars(obj).items() if not k.startswith("_")
        }
    return str(obj)


class YahooClient:
    """League-scoped Yahoo access with caching."""

    def __init__(self, cfg: Config, conn: Database | None = None):
        self.cfg = cfg
        self.conn = conn or db.init_db(cfg.db_path)
        self._query = None
        self._league_key: str | None = None
        self.idmap = IdMapper(self.conn, cfg.get("paths.manual_id_overrides"))

    # -- auth / connection ---------------------------------------------------

    @property
    def query(self):
        """Lazily build the yfpy query object so offline paths never authenticate."""
        if self._query is None:
            from yfpy.query import YahooFantasySportsQuery

            league_id = str(self.cfg.require("league.league_id"))
            env_dir = Path(self.cfg.get("paths.env_dir", "."))
            self._query = YahooFantasySportsQuery(
                league_id=league_id,
                game_code=self.cfg.get("league.game_code", "nfl"),
                game_id=self.cfg.get("league.game_id"),
                env_file_location=env_dir,
                # Only where a human will reuse the token. yfpy writes the
                # access token, the REFRESH token and the GUID into
                # <env_dir>/.env, and on a CI runner that is inside the checked
                # out workspace - one `path:` line away from being uploaded as
                # an artifact, and destroyed with the runner anyway.
                save_token_data_to_env_file=sys.stdin.isatty(),
                # Defaults to whether anyone is actually there to click it. A
                # scheduled run with no TTY would otherwise block on an OAuth
                # browser prompt until the job timed out.
                browser_callback=bool(
                    self.cfg.get("league.browser_callback", sys.stdin.isatty())
                ),
                retries=3,
                backoff=1,
            )
        return self._query

    @property
    def league_key(self) -> str:
        if self._league_key is None:
            season = self.cfg.get("league.season")
            self._league_key = (
                self.query.get_league_key(int(season)) if season
                else self.query.get_league_key()
            )
        return self._league_key

    def resolve_season(self) -> int:
        season = self.cfg.get("league.season")
        if season:
            return int(season)
        game = serialize(self.query.get_current_game_info())
        return int(game.get("season") or 0)

    # -- generic cached fetch ------------------------------------------------

    def _cached(self, cache_key: str, fetch, force: bool = False) -> tuple[Any, bool]:
        """Run `fetch`, caching the result. Returns (payload, from_cache).

        The cache here is a *failure fallback*, not a TTL: Yahoo is the source of
        truth for the league, so we always try the network first and only serve
        a stored copy when the call fails. That way a job never crashes because
        Yahoo blipped (spec 10, last acceptance test). `force` is accepted for
        call-site symmetry with the other sources.
        """
        try:
            payload = serialize(fetch())
            db.cache_put(self.conn, cache_key, "yahoo", payload)
            return payload, False
        except Exception as exc:
            cached = db.cache_get(self.conn, cache_key)
            if cached is None:
                raise
            payload, fetched_at = cached
            log.warning(
                "Yahoo fetch failed for %s (%s); using cache from %s", cache_key, exc, fetched_at
            )
            return payload, True

    # -- league settings -----------------------------------------------------

    def fetch_league_settings(self, force: bool = False) -> dict[str, Any]:
        key = f"yahoo:settings:{self.league_key}"
        payload, from_cache = self._cached(key, self.query.get_league_settings, force)
        if not from_cache:
            self.conn.execute(
                "INSERT INTO league_settings(league_key, season, settings_json, fetched_at) "
                "VALUES (?,?,?,?) ON CONFLICT(league_key) DO UPDATE SET "
                "settings_json=excluded.settings_json, fetched_at=excluded.fetched_at, "
                "season=excluded.season",
                (self.league_key, self.resolve_season(), json.dumps(payload), db.utcnow()),
            )
            self.conn.commit()
        return payload

    def load_settings(self) -> dict[str, Any]:
        """Read stored settings without touching the network."""
        row = self.conn.execute(
            "SELECT settings_json FROM league_settings WHERE league_key=?",
            (self.league_key,),
        ).fetchone()
        if row is None:
            return self.fetch_league_settings()
        return json.loads(row["settings_json"])

    def scoring(self) -> LeagueScoring:
        return build_from_yahoo(self.load_settings())

    # -- roster construction -------------------------------------------------

    def roster_slots(self) -> dict[str, int]:
        """Starting slots -> count, e.g. {"QB":1,"RB":2,"WR":2,"TE":1,"W/R/T":1}."""
        settings = self.load_settings()
        raw = settings.get("roster_positions") or []
        slots: dict[str, int] = {}
        for entry in raw:
            rp = entry.get("roster_position", entry) if isinstance(entry, dict) else {}
            pos = str(rp.get("position") or "")
            count = int(rp.get("count") or 0)
            if pos:
                slots[pos] = slots.get(pos, 0) + count
        return slots

    def starting_slots(self) -> dict[str, int]:
        return {k: v for k, v in self.roster_slots().items() if k not in BENCH_SLOTS}

    def bench_size(self) -> int:
        return sum(v for k, v in self.roster_slots().items() if k in BENCH_SLOTS)

    def num_teams(self) -> int:
        settings = self.load_settings()
        for key in ("num_teams", "number_of_teams"):
            if settings.get(key):
                return int(settings[key])
        meta = serialize(self.query.get_league_metadata())
        return int(meta.get("num_teams") or 12)

    def waiver_config(self) -> dict[str, Any]:
        """FAAB vs. priority, and the budget, straight from league settings."""
        s = self.load_settings()
        uses_faab = str(s.get("uses_faab", "0")) in ("1", "true", "True")
        return {
            "uses_faab": uses_faab,
            "faab_budget": int(s.get("faab_budget") or 100) if uses_faab else None,
            "waiver_type": s.get("waiver_type"),
            "waiver_rule": s.get("waiver_rule"),
            "waiver_time": s.get("waiver_time"),
            "trade_end_date": s.get("trade_end_date"),
            "playoff_start_week": _as_int(s.get("playoff_start_week")),
            "num_playoff_teams": _as_int(s.get("num_playoff_teams")),
        }

    # -- teams & rosters -----------------------------------------------------

    def fetch_teams(self, force: bool = False) -> list[dict[str, Any]]:
        key = f"yahoo:teams:{self.league_key}"
        payload, _ = self._cached(key, self.query.get_league_teams, force)
        return payload or []

    def store_teams(self, teams: Iterable[dict[str, Any]], season: int) -> int:
        """Persist the team list and each manager's remaining FAAB.

        Remaining budget is the sharpest input the bid model has - a manager
        sitting on $2 is not a rival however aggressively he normally bids - so
        it is worth a table of its own rather than being re-derived from the
        transaction log, which only shows what was spent, not what was carried.
        """
        stored = 0
        for team in teams:
            team_id = team.get("team_id")
            if team_id in (None, ""):
                continue
            self.conn.execute(
                "INSERT INTO team_budgets(league_key, season, team_key, team_name, "
                "faab_balance, waiver_priority, fetched_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(league_key, season, team_key) DO UPDATE SET "
                "team_name=excluded.team_name, faab_balance=excluded.faab_balance, "
                "waiver_priority=excluded.waiver_priority, fetched_at=excluded.fetched_at",
                (
                    self.league_key, int(season), str(team_id), team.get("name"),
                    _as_int(team.get("faab_balance")),
                    _as_int(team.get("waiver_priority")),
                    db.utcnow(),
                ),
            )
            stored += 1
        self.conn.commit()
        return stored

    def my_team_id(self) -> int | None:
        """The configured team id, or auto-detect via the authenticated user."""
        configured = self.cfg.get("league.my_team_id")
        if configured:
            return int(configured)
        try:
            for team in self.fetch_teams():
                if str(team.get("is_owned_by_current_login", "0")) in ("1", "true"):
                    team_id = team.get("team_id")
                    if team_id is not None:
                        return int(team_id)
        except Exception as exc:
            log.warning("Could not auto-detect your team id: %s", exc)
        return None

    def fetch_roster(self, team_id: int, week: int | str = "current",
                     force: bool = False) -> list[dict[str, Any]]:
        key = f"yahoo:roster:{self.league_key}:{team_id}:{week}"
        payload, _ = self._cached(
            key, lambda: self.query.get_team_roster_player_info_by_week(team_id, week), force
        )
        return payload or []

    def store_roster(self, team_id: int, week: int, players: Iterable[dict[str, Any]],
                     team_name: str | None = None) -> int:
        stored = 0
        for p in players:
            key = self._upsert_from_yahoo_player(p)
            if not key:
                continue
            self.conn.execute(
                "INSERT INTO rosters(league_key, team_key, team_name, player_key, "
                "selected_pos, week, fetched_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(league_key, team_key, player_key, week) DO UPDATE SET "
                "selected_pos=excluded.selected_pos, fetched_at=excluded.fetched_at",
                (
                    self.league_key, str(team_id), team_name, key,
                    p.get("selected_position_value") or _dig(p, ["selected_position", "position"]),
                    week, db.utcnow(),
                ),
            )
            stored += 1
        self.conn.commit()
        return stored

    # -- players / free agents ----------------------------------------------

    def fetch_free_agents(self, count: int = 200, position: str | None = None,
                          force: bool = False) -> list[dict[str, Any]]:
        """Available players, most-relevant first.

        Uses the raw Yahoo `status=FA` filter rather than walking the whole
        player universe, which would be hundreds of paginated calls.
        """
        cache_key = f"yahoo:fa:{self.league_key}:{position or 'ALL'}:{count}"

        def _fetch():
            collected: list[Any] = []
            page = 25  # Yahoo caps a players collection at 25 per request
            for start in range(0, count, page):
                filters = ["status=FA", "sort=AR", f"count={page}", f"start={start}"]
                if position:
                    filters.append(f"position={position}")
                url = (
                    f"{YAHOO_API_BASE}/league/{self.league_key}/players;"
                    + ";".join(filters)
                    + "?format=json"
                )
                from yfpy.models import Player

                batch = self.query.query(
                    url, ["league", "players"], data_type_class=Player
                )
                if not batch:
                    break
                collected.extend(batch)
                if len(batch) < page:
                    break
            return collected

        payload, _ = self._cached(cache_key, _fetch, force)
        return payload or []

    def store_free_agents(self, players: Iterable[dict[str, Any]], week: int) -> int:
        stored = 0
        for p in players:
            key = self._upsert_from_yahoo_player(p)
            if not key:
                continue
            self.conn.execute(
                "INSERT INTO free_agents(league_key, player_key, pct_owned, week, fetched_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(league_key, player_key, week) DO UPDATE SET "
                "pct_owned=excluded.pct_owned, fetched_at=excluded.fetched_at",
                (self.league_key, key, _as_float(p.get("percent_owned_value")), week, db.utcnow()),
            )
            stored += 1
        self.conn.commit()
        return stored

    # -- draft ---------------------------------------------------------------

    def fetch_draft_results(self, force: bool = True) -> list[dict[str, Any]]:
        """Live during the draft, so this defaults to bypassing cache."""
        key = f"yahoo:draft:{self.league_key}"
        payload, _ = self._cached(key, self.query.get_league_draft_results, force)
        return payload or []

    def store_draft_results(self, results: Iterable[dict[str, Any]]) -> int:
        """Persist picks. Player keys are resolved from the Yahoo player key."""
        stored = 0
        for r in results:
            yahoo_player_key = r.get("player_key")
            if not yahoo_player_key:
                continue
            player_key = self._player_key_from_yahoo_key(yahoo_player_key)
            self.conn.execute(
                "INSERT INTO draft_picks(league_key, pick, round, team_key, player_key, "
                "source, recorded_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(league_key, pick) DO UPDATE SET "
                "player_key=excluded.player_key, team_key=excluded.team_key, "
                "round=excluded.round, recorded_at=excluded.recorded_at",
                (
                    self.league_key, _as_int(r.get("pick")), _as_int(r.get("round")),
                    r.get("team_key"), player_key, "yahoo", db.utcnow(),
                ),
            )
            stored += 1
        self.conn.commit()
        return stored

    def draft_settings(self) -> dict[str, Any]:
        s = self.load_settings()
        return {
            "draft_type": s.get("draft_type"),
            "is_auction_draft": str(s.get("is_auction_draft", "0")) in ("1", "true"),
            "draft_time": s.get("draft_time"),
            "draft_pick_time": s.get("draft_pick_time"),
        }

    # -- transactions --------------------------------------------------------

    def fetch_transactions(self, force: bool = False) -> list[dict[str, Any]]:
        key = f"yahoo:txns:{self.league_key}"
        payload, _ = self._cached(key, self.query.get_league_transactions, force)
        return payload or []

    def store_transactions(self, txns: Iterable[dict[str, Any]]) -> int:
        stored = 0
        for t in txns:
            txn_id = str(t.get("transaction_id") or t.get("transaction_key") or "")
            if not txn_id:
                continue
            self.conn.execute(
                "INSERT INTO transactions(league_key, txn_id, type, timestamp, payload_json) "
                "VALUES (?,?,?,?,?) ON CONFLICT(league_key, txn_id) DO UPDATE SET "
                "type=excluded.type, timestamp=excluded.timestamp, payload_json=excluded.payload_json",
                (self.league_key, txn_id, t.get("type"), str(t.get("timestamp") or ""),
                 json.dumps(t)),
            )
            stored += 1
        self.conn.commit()
        return stored

    # -- helpers -------------------------------------------------------------

    def _upsert_from_yahoo_player(self, p: dict[str, Any]) -> str | None:
        """Register a Yahoo player payload in the canonical players table."""
        name = p.get("full_name") or _dig(p, ["name", "full"])
        if not name:
            return None
        position = (
            p.get("primary_position")
            or p.get("display_position")
            or _dig(p, ["selected_position", "position"])
        )
        team = p.get("editorial_team_abbr")
        bye = p.get("bye") or _dig(p, ["bye_weeks", "week"])
        return self.idmap.upsert_player(
            full_name=name,
            position=position,
            team=team,
            bye_week=_as_int(bye),
            status=p.get("status") or None,
            first_name=p.get("first_name") or _dig(p, ["name", "first"]),
            last_name=p.get("last_name") or _dig(p, ["name", "last"]),
            yahoo_id=str(p.get("player_id")) if p.get("player_id") else None,
            yahoo_key=p.get("player_key"),
        )

    def _player_key_from_yahoo_key(self, yahoo_player_key: str) -> str | None:
        """Map "449.p.12345" onto our canonical key via the stored player table."""
        yahoo_id = str(yahoo_player_key).split(".")[-1]
        row = self.conn.execute(
            "SELECT player_key FROM players WHERE yahoo_id=? OR yahoo_key=?",
            (yahoo_id, yahoo_player_key),
        ).fetchone()
        return row["player_key"] if row else None


def _dig(data: Any, path: list[str]) -> Any:
    node = data
    for part in path:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
