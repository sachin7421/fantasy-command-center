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
        self._index: Any = None
        #: Yahoo responses for THIS RUN only. Never written to disk, never
        #: shared between runs - a second YahooClient starts empty, which is
        #: what makes "for the duration of a run" true rather than aspirational.
        self._memo: dict[str, Any] = {}
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
        """Run `fetch` once per run, holding the result in memory.

        Returns (payload, from_memory).

        This used to write every Yahoo response to `source_cache` and serve the
        stored copy whenever a later call failed. The API agreement signed
        2026-09-10 forbids that: Yahoo Fantasy data lives in memory for the
        duration of a run and never reaches disk. `db.cache_put` now refuses
        the source outright, so the old path cannot be restored by accident.

        Two consequences worth stating plainly.

        The memo is not an optimisation, it is the cost control. With disk
        caching gone, an unmemoised client would re-fetch on every caller -
        `doctor`, `sync` and the job itself each ask for settings - multiplying
        API calls by however many places happen to want the same thing.

        And a Yahoo outage is now fatal to the run rather than survivable. This
        REVERSES this project's own engineering standard 4, which says an
        external source that fails should fall back to cached data with a
        warning. There is no longer anything to fall back to, and that is the
        correct outcome anyway: a stale roster produces confident, wrong advice
        about players who are no longer on it, which is worse than an error.
        `force` re-fetches, discarding the memo.
        """
        if not force and cache_key in self._memo:
            return self._memo[cache_key], True
        try:
            payload = serialize(fetch())
        except Exception as exc:
            log.error(
                "Yahoo fetch failed for %s (%s). There is no cached copy - the "
                "API agreement forbids storing one - so this run cannot "
                "continue on stale data.",
                cache_key, exc,
            )
            raise
        self._memo[cache_key] = payload
        return payload, False

    def forget_yahoo_data(self) -> int:
        """Drop every Yahoo response this run is holding. Returns how many.

        Called at the end of a run, and by `fcc purge-yahoo`. Not strictly
        required - the process exiting achieves the same thing - but a
        long-lived process (the dashboard) can otherwise hold a roster in
        memory for hours after it was last needed.
        """
        held = len(self._memo)
        self._memo.clear()
        return held

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

    def fetch_draft_results(self, force: bool = True) -> list[dict[str, Any]]:
        """Live during the draft, so this defaults to bypassing cache."""
        key = f"yahoo:draft:{self.league_key}"
        payload, _ = self._cached(key, self.query.get_league_draft_results, force)
        return payload or []

    def draft_settings(self) -> dict[str, Any]:
        s = self.load_settings()
        return {
            "draft_type": s.get("draft_type"),
            "is_auction_draft": str(s.get("is_auction_draft", "0")) in ("1", "true"),
            "draft_time": s.get("draft_time"),
            "draft_pick_time": s.get("draft_pick_time"),
        }

    # -- the snapshot: Yahoo state for this run, never written down ---------

    def new_snapshot(self, season: int, week: int):
        """An empty snapshot for this league."""
        from src.yahoo_snapshot import LeagueSnapshot

        return LeagueSnapshot(league_key=self.league_key, season=int(season),
                              week=int(week))

    def collect_teams(self, snapshot, teams: Iterable[dict[str, Any]]):
        """Remaining FAAB and waiver priority, per team.

        Budget is the sharpest single input the bid model has - a manager
        sitting on $2 is not a rival however aggressively he normally bids -
        and it used to be worth a table of its own. It is worth exactly as much
        held in memory; it just cannot outlive the run.
        """
        from src.yahoo_snapshot import TeamBudget

        for team in teams:
            team_id = team.get("team_id")
            if team_id in (None, ""):
                continue
            snapshot.budgets[str(team_id)] = TeamBudget(
                team_key=str(team_id),
                # serialize() again, not redundantly: yfpy hands back Team.name
                # as BYTES, and a caller that passes raw models rather than
                # already-serialised dicts would otherwise put b'Butt Fumblers'
                # into every report. Idempotent for a str.
                team_name=serialize(team.get("name")),
                faab_balance=_as_int(team.get("faab_balance")),
                waiver_priority=_as_int(team.get("waiver_priority")),
            )
        return snapshot

    def collect_roster(self, snapshot, team_id: Any, players: Iterable[dict[str, Any]],
                       team_name: str | None = None):
        """One team's roster, as our player keys."""
        from src.yahoo_snapshot import RosterSpot

        for p in players:
            key = self._resolve_yahoo_player(p)
            if not key:
                continue
            snapshot.rosters.append(RosterSpot(
                team_key=str(team_id),
                team_name=team_name,
                player_key=key,
                selected_pos=(p.get("selected_position_value")
                              or _dig(p, ["selected_position", "position"])),
            ))
        snapshot.unmatched = list(self.index.unmatched)
        return snapshot

    def collect_free_agents(self, snapshot, players: Iterable[dict[str, Any]]):
        for p in players:
            key = self._resolve_yahoo_player(p)
            if key:
                snapshot.free_agents.append(key)
        snapshot.unmatched = list(self.index.unmatched)
        return snapshot

    def collect_transactions(self, snapshot, txns: Iterable[dict[str, Any]]):
        """The raw log, read once so bid behaviour can be learned from it.

        What survives the run is the learned coefficient, not this - a shrunk
        dollars-per-point number from which no Yahoo fact is recoverable.
        """
        snapshot.transactions.extend(txns)
        return snapshot

    def collect_snapshot(self, season: int, week: int,
                         teams: Iterable[dict[str, Any]] | None = None):
        """Everything, in one call. Used by `fcc sync-league` and the jobs."""
        snapshot = self.new_snapshot(season, week)
        team_list = list(teams) if teams is not None else self.fetch_teams()
        self.collect_teams(snapshot, team_list)
        return snapshot

    # -- transactions --------------------------------------------------------

    def fetch_transactions(self, force: bool = False) -> list[dict[str, Any]]:
        key = f"yahoo:txns:{self.league_key}"
        payload, _ = self._cached(key, self.query.get_league_transactions, force)
        return payload or []

    @property
    def index(self):
        """Yahoo player -> our player key, built once per run and held in memory.

        Replaces `_upsert_from_yahoo_player`, which registered every Yahoo
        player into our `players` table along with his Yahoo id and key. That
        was a write of Yahoo identifiers to disk on every roster sync, which
        the API agreement forbids.

        Resolution only, now: it reads what we already have and creates
        nothing. Where Sleeper published a Yahoo cross-reference id the join is
        exact; otherwise names are matched, and anything unmatched is reported
        rather than dropped.
        """
        if self._index is None:
            from src.yahoo_snapshot import YahooIdIndex

            self._index = YahooIdIndex(self.conn)
        return self._index

    def _resolve_yahoo_player(self, p: dict[str, Any]) -> str | None:
        """Our player key for a Yahoo player payload. Writes nothing."""
        return self.index.resolve(p)

    def _player_key_from_yahoo_key(self, yahoo_player_key: str) -> str | None:
        """Map "449.p.12345" onto our canonical key, without storing anything."""
        return self.index.resolve({"player_key": yahoo_player_key})


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
