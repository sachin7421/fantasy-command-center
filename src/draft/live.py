"""Live draft state: Yahoo polling with a manual fallback (spec 5.2, 5.3).

The tracker owns "who has been drafted, and by whom". It prefers Yahoo's live
draft results, but every operation works identically when Yahoo polling fails
and picks are entered by hand - which is the difference between a useful tool
and a paperweight if the API goes sideways mid-draft.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections.abc import Callable

from src import db
from src.vorp import Board, PlayerValue
from src.storage import Database

log = logging.getLogger(__name__)


@dataclass
class Pick:
    pick: int
    round: int
    team_key: str | None
    player_key: str | None
    source: str = "yahoo"


@dataclass
class DraftState:
    league_key: str
    num_teams: int
    rounds: int
    picks: dict[int, Pick] = field(default_factory=dict)

    @property
    def drafted_keys(self) -> set[str]:
        return {p.player_key for p in self.picks.values() if p.player_key}

    @property
    def next_pick(self) -> int:
        return (max(self.picks) + 1) if self.picks else 1

    @property
    def total_picks(self) -> int:
        return self.num_teams * self.rounds

    def roster_of(self, team_key: str) -> list[str]:
        return [
            p.player_key
            for p in sorted(self.picks.values(), key=lambda x: x.pick)
            if p.team_key == team_key and p.player_key
        ]

    def team_key_for_pick(self, overall: int, snake: bool = True) -> int:
        """Which draft slot owns an overall pick number."""
        rnd = (overall - 1) // self.num_teams + 1
        slot = (overall - 1) % self.num_teams + 1
        if snake and rnd % 2 == 0:
            slot = self.num_teams - slot + 1
        return slot


class DraftTracker:
    """Maintains draft state from Yahoo, from manual entry, or both."""

    def __init__(
        self,
        conn: Database,
        league_key: str,
        num_teams: int,
        rounds: int,
        yahoo_client=None,
    ):
        self.conn = conn
        self.league_key = league_key
        self.yahoo = yahoo_client
        self.state = DraftState(league_key=league_key, num_teams=num_teams, rounds=rounds)
        self._load_from_db()
        self.last_sync_ok: bool | None = None
        self.last_error: str | None = None

    # -- persistence ---------------------------------------------------------

    def _load_from_db(self) -> None:
        for row in self.conn.execute(
            "SELECT pick, round, team_key, player_key, source FROM draft_picks "
            "WHERE league_key=? ORDER BY pick",
            (self.league_key,),
        ):
            self.state.picks[row["pick"]] = Pick(
                pick=row["pick"], round=row["round"], team_key=row["team_key"],
                player_key=row["player_key"], source=row["source"],
            )

    def _store(self, pick: Pick) -> None:
        self.conn.execute(
            "INSERT INTO draft_picks(league_key, pick, round, team_key, player_key, "
            "source, recorded_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(league_key, pick) DO UPDATE SET round=excluded.round, "
            "team_key=excluded.team_key, player_key=excluded.player_key, "
            "source=excluded.source, recorded_at=excluded.recorded_at",
            (
                self.league_key, pick.pick, pick.round, pick.team_key,
                pick.player_key, pick.source, db.utcnow(),
            ),
        )
        self.conn.commit()

    # -- manual entry (the fallback that must always work) -------------------

    def skip_to(self, target_pick: int) -> int:
        """Fill the gap up to `target_pick` with picks whose player is unknown.

        The live-draft reality this exists for: three players went while you
        were looking at your own roster and you caught one name. Without a way
        to say so, the app's pick counter falls behind the real draft and every
        round-based rule - survival probability, deferral, need - is computed
        for the wrong pick. Anonymous picks advance the count and the snake
        order without claiming anyone is off the board, so the players you did
        not catch stay available and cost you only themselves.

        Returns the number of placeholder picks written.
        """
        written = 0
        while self.state.next_pick < target_pick:
            self.record_pick(None, source="skipped")
            written += 1
        return written

    def record_pick(
        self,
        player_key: str | None,
        pick: int | None = None,
        team_key: str | None = None,
        source: str = "manual",
    ) -> Pick:
        """Mark a player drafted. Used by tap-to-mark in the Streamlit board."""
        pick_number = pick or self.state.next_pick
        rnd = (pick_number - 1) // self.state.num_teams + 1
        if team_key is None:
            team_key = str(self.state.team_key_for_pick(pick_number))
        entry = Pick(pick_number, rnd, team_key, player_key, source)
        self.state.picks[pick_number] = entry
        self._store(entry)
        return entry

    def undo_last(self) -> Pick | None:
        """Undo support, because mis-taps happen in a live draft."""
        if not self.state.picks:
            return None
        last = max(self.state.picks)
        removed = self.state.picks.pop(last)
        self.conn.execute(
            "DELETE FROM draft_picks WHERE league_key=? AND pick=?", (self.league_key, last)
        )
        self.conn.commit()
        return removed

    def reset(self) -> None:
        self.state.picks.clear()
        self.conn.execute("DELETE FROM draft_picks WHERE league_key=?", (self.league_key,))
        self.conn.commit()

    # -- Yahoo sync ----------------------------------------------------------

    def sync_from_yahoo(self) -> int:
        """Pull draft results from Yahoo. Returns the number of new picks seen.

        Never raises: a failure here must not stop the draft board from working,
        it just flips `last_sync_ok` so the UI can show a degraded banner.
        """
        if self.yahoo is None:
            self.last_sync_ok = False
            self.last_error = "Yahoo client not configured"
            return 0
        try:
            results = self.yahoo.fetch_draft_results(force=True)
        except Exception as exc:
            self.last_sync_ok = False
            self.last_error = str(exc)
            log.warning("Draft sync failed, staying on last known state: %s", exc)
            return 0

        new = 0
        for r in results:
            pick_no = _as_int(r.get("pick"))
            if not pick_no:
                continue
            yahoo_player_key = r.get("player_key")
            if not yahoo_player_key:
                continue  # pick not made yet
            player_key = self.yahoo._player_key_from_yahoo_key(yahoo_player_key)
            existing = self.state.picks.get(pick_no)
            if existing and existing.player_key == player_key:
                continue
            entry = Pick(
                pick=pick_no,
                round=_as_int(r.get("round")) or ((pick_no - 1) // self.state.num_teams + 1),
                team_key=r.get("team_key"),
                player_key=player_key,
                source="yahoo",
            )
            self.state.picks[pick_no] = entry
            self._store(entry)
            new += 1

        self.last_sync_ok = True
        self.last_error = None
        return new

    def poll(
        self,
        on_my_pick: Callable[[DraftState], None],
        my_slot: int,
        interval: float = 10.0,
        max_seconds: float | None = None,
    ) -> None:
        """Blocking poll loop for CLI use (spec 5.2, ~10s cadence)."""
        started = time.monotonic()
        notified: set[int] = set()
        while True:
            self.sync_from_yahoo()
            upcoming = self.state.next_pick
            if (
                self.state.team_key_for_pick(upcoming) == my_slot
                and upcoming not in notified
            ):
                notified.add(upcoming)
                on_my_pick(self.state)
            if upcoming > self.state.total_picks:
                return
            if max_seconds and (time.monotonic() - started) > max_seconds:
                return
            time.sleep(interval)


def resolve_player(board: Board, text: str) -> list[PlayerValue]:
    """Search the board by name, for the manual mark-drafted search box."""
    from src.idmap import normalize_name

    needle = normalize_name(text)
    if not needle:
        return []
    exact = [p for p in board.players if normalize_name(p.name) == needle]
    if exact:
        return exact
    return [p for p in board.players if needle in normalize_name(p.name)][:15]


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
