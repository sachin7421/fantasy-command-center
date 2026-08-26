"""Run whole drafts against the real board, and check every pick.

The draft is fourteen days away, runs for three hours, gives ninety seconds a
pick, and cannot be repeated. Individually the pieces are tested; nobody has
ever walked the whole thing. That gap is where the worst bug of the project so
far lived - `disabled=not is_mine` was correct in isolation and locked the user
out of his own pick in use.

So this drives the SAME code the dashboard drives - DraftTracker for state and
persistence, DraftRecommender for advice - through 180 picks, with eleven
opponents who behave like people rather than like a sorted list, and asserts
after every single pick that the state is still sane.

    python tools/simulate_draft.py                  # 12 slots x 3 seeds
    python tools/simulate_draft.py --slot 3         # just ours, verbosely
    python tools/simulate_draft.py --seeds 10       # more opponents' luck

Nothing here can touch the league database. The real board is COPIED into a
temporary SQLite file and every write goes there - a test run once wrote
fixture rows into the live Supabase tables, and that is not repeatable either.
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db, vorp
from src.config import Config
from src.draft.live import DraftTracker
from src.draft.recommender import DraftRecommender, RosterState
from src.draft.survival import DraftPosition
from src.lineup_solver import best_lineup

ROUNDS = 15
BENCH_CAP = {"QB": 3, "RB": 7, "WR": 8, "TE": 3, "DEF": 2, "K": 2}


# --- a private copy of the real board ----------------------------------------

TABLES = ("players", "projections_blended", "adp", "injuries")


def snapshot(source: db.Database, path: Path) -> db.Database:
    """Copy the live board into a throwaway SQLite file.

    Read-only against the league database, and every write in the simulation
    lands in the copy. This is the guard, not a convenience: the alternative is
    a simulation that records 180 picks into the real draft.
    """
    dest = db.init_db(path, force_sqlite=True)
    for table in TABLES:
        rows = source.fetchall(f"SELECT * FROM {table}")
        if not rows:
            continue
        columns = list(rows[0].keys())
        placeholders = ",".join("?" for _ in columns)
        sql = (f"INSERT OR REPLACE INTO {table}({','.join(columns)}) "
               f"VALUES ({placeholders})")
        for row in rows:
            dest.execute(sql, tuple(row[c] for c in columns))
    dest.commit()
    return dest


# --- opponents ---------------------------------------------------------------

class Opponent:
    """One of the eleven other managers.

    Real drafts are not an ADP re-sort, and a simulation where they are flatters
    the recommender: every faller it is proud of catching only fell because the
    model assumed everyone else drafts identically. These behave differently
    from each other and from the market.
    """

    def __init__(self, style: str, rng: random.Random, slots: dict[str, int]):
        self.style = style
        self.rng = rng
        self.slots = slots
        self.roster: list = []

    def counts(self) -> Counter:
        return Counter(p.position for p in self.roster)

    def _legal(self, available: list) -> list:
        counts = self.counts()
        return [p for p in available if counts[p.position] < BENCH_CAP.get(p.position, 4)]

    def pick(self, available: list, round_number: int, last_position: str | None):
        pool = self._legal(available) or available

        if self.style == "adp":
            # Follows the market, with the noise a human introduces.
            ranked = sorted(pool, key=lambda p: (p.adp if p.adp else 9999))
            window = ranked[: max(1, min(6, len(ranked)))]
            return self.rng.choice(window)

        if self.style == "points":
            # Drafts the biggest projection, ignoring position entirely. Someone
            # in every league does this, and it is what the board must beat.
            return max(pool, key=lambda p: p.points)

        if self.style == "need":
            counts = self.counts()
            missing = [
                pos for pos, n in self.slots.items()
                if pos not in ("W/R/T", "BN", "IR") and counts[pos] < n
            ]
            if missing:
                candidates = [p for p in pool if p.position in missing]
                if candidates:
                    return min(candidates, key=lambda p: (p.adp if p.adp else 9999))
            return min(pool, key=lambda p: (p.adp if p.adp else 9999))

        if self.style == "reacher":
            # Takes his guy twenty picks early. This is what actually removes a
            # player the survival model said was safe.
            ranked = sorted(pool, key=lambda p: (p.adp if p.adp else 9999))
            depth = min(len(ranked) - 1, self.rng.randint(0, 20))
            return ranked[depth]

        if self.style == "runner":
            # Joins position runs, which is how a tier empties in four picks.
            if last_position and self.rng.random() < 0.55:
                same = [p for p in pool if p.position == last_position]
                if same:
                    return min(same, key=lambda p: (p.adp if p.adp else 9999))
            ranked = sorted(pool, key=lambda p: (p.adp if p.adp else 9999))
            return ranked[0]

        raise ValueError(f"unknown style {self.style}")


STYLES = ["adp", "need", "reacher", "runner", "points",
          "adp", "need", "adp", "reacher", "runner", "adp"]


# --- the checks --------------------------------------------------------------

class Failure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def run_one(board: vorp.Board, conn: db.Database, slot: int, seed: int,
            slots: dict[str, int], num_teams: int, verbose: bool = False) -> dict:
    """One complete draft, checked at every pick."""
    rng = random.Random(seed * 1000 + slot)
    league_key = f"sim.{slot}.{seed}"

    tracker = DraftTracker(conn, league_key, num_teams, ROUNDS)
    tracker.reset()

    position = DraftPosition(num_teams=num_teams, draft_slot=slot, rounds=ROUNDS)
    recommender = DraftRecommender(board, position)
    opponents = {
        s: Opponent(STYLES[(s - 1) % len(STYLES)], rng, slots)
        for s in range(1, num_teams + 1) if s != slot
    }
    my_roster = RosterState(slots)
    last_position: str | None = None
    advice_log = []

    for overall in range(1, num_teams * ROUNDS + 1):
        drafted = tracker.state.drafted_keys
        available = board.available(drafted)
        check(bool(available), f"pick {overall}: the board ran out of players")

        owner = tracker.state.team_key_for_pick(overall)

        if owner == slot:
            round_number = position.current_round(overall)
            picks = recommender.recommend(drafted, my_roster, overall, top_n=5)

            # --- the recommender's contract, checked live -----------------
            check(bool(picks), f"pick {overall}: no recommendation offered")
            scores = [p.score for p in picks]
            check(scores == sorted(scores, reverse=True),
                  f"pick {overall}: recommendations are not best-first")
            check(all(p.score == p.score for p in picks),
                  f"pick {overall}: a NaN score")
            for rec in picks:
                check(rec.player.player_key not in drafted,
                      f"pick {overall}: recommended {rec.player.name}, already drafted")
                if rec.survival is not None:
                    check(0.0 <= rec.survival <= 1.0,
                          f"pick {overall}: survival {rec.survival} is not a probability")

            chosen = picks[0].player
            my_roster.players.append(chosen)
            advice_log.append((overall, round_number, chosen, picks[0]))
            if verbose:
                surv = picks[0].survival
                print(f"    R{round_number:<2} p{overall:<4} {chosen.name:<24}"
                      f" {chosen.position:<4} vorp {chosen.vorp:7.1f}"
                      f"  {'' if surv is None else f'survival {surv:.0%}'}")
        else:
            chosen = opponents[owner].pick(available, overall, last_position)
            opponents[owner].roster.append(chosen)

        check(chosen.player_key not in drafted,
              f"pick {overall}: {chosen.name} was drafted twice")
        tracker.record_pick(chosen.player_key, pick=overall, team_key=str(owner))
        last_position = chosen.position

        check(tracker.state.next_pick == overall + 1,
              f"pick {overall}: the pick counter is at {tracker.state.next_pick}")

    # --- end of draft -------------------------------------------------------
    all_keys = [p.player_key for p in tracker.state.picks.values() if p.player_key]
    check(len(all_keys) == len(set(all_keys)), "a player was drafted by two teams")
    check(len(all_keys) == num_teams * ROUNDS,
          f"{len(all_keys)} picks recorded, expected {num_teams * ROUNDS}")

    mine = my_roster.players
    check(len(mine) == ROUNDS, f"my roster has {len(mine)} players, expected {ROUNDS}")

    lineup = best_lineup(mine, slots)
    check(lineup.is_complete,
          "my roster cannot field a legal starting lineup - empty: "
          f"{lineup.empty_slots}, from {dict(Counter(p.position for p in mine))}")

    scores = {}
    for other, opp in opponents.items():
        scores[other] = best_lineup(opp.roster, slots).total
    scores[slot] = lineup.total

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    my_rank = [s for s, _ in ranked].index(slot) + 1

    return {
        "slot": slot, "seed": seed,
        "points": lineup.total,
        "rank": my_rank,
        "best_rival": max(v for k, v in scores.items() if k != slot),
        "median_rival": sorted(v for k, v in scores.items() if k != slot)[5],
        "roster": dict(Counter(p.position for p in mine)),
        "advice": advice_log,
    }


# --- stress scenarios --------------------------------------------------------

def check_resilience(board: vorp.Board, conn: db.Database, slots: dict[str, int],
                     num_teams: int) -> list[str]:
    """The things that happen to a real draft, not to a simulated one."""
    notes = []
    league_key = "sim.resilience"

    tracker = DraftTracker(conn, league_key, num_teams, ROUNDS)
    tracker.reset()
    pool = sorted(board.players, key=lambda p: (p.adp if p.adp else 9999))

    for i in range(20):
        tracker.record_pick(pool[i].player_key)

    # 1. the browser is refreshed mid-draft
    reloaded = DraftTracker(conn, league_key, num_teams, ROUNDS)
    check(reloaded.state.drafted_keys == tracker.state.drafted_keys,
          "a refresh mid-draft lost picks")
    check(reloaded.state.next_pick == tracker.state.next_pick,
          "a refresh mid-draft moved the pick counter")
    notes.append("refresh mid-draft: state restored exactly")

    # 2. three picks went by unseen, and only the count is known
    before = reloaded.state.drafted_keys
    written = reloaded.skip_to(24)
    check(written == 3, f"skip_to wrote {written} placeholders, expected 3")
    check(reloaded.state.drafted_keys == before,
          "skipping picks wrongly removed players from the board")
    check(reloaded.state.next_pick == 24, "skip_to left the counter wrong")
    notes.append("3 unseen picks: counter advanced, nobody wrongly removed")

    # 3. a mis-tap is undone
    target = pool[40]
    reloaded.record_pick(target.player_key)
    check(target.player_key in reloaded.state.drafted_keys, "recording a pick did nothing")
    reloaded.undo_last()
    check(target.player_key not in reloaded.state.drafted_keys, "undo did not restore him")
    notes.append("mis-tap undone: player returns to the board")

    # 4. a pick is corrected out of order
    reloaded.record_pick(pool[50].player_key, pick=5)
    check(pool[50].player_key in reloaded.state.drafted_keys,
          "an out-of-order correction was lost")
    notes.append("out-of-order correction: accepted and persisted")

    # 5. the same player entered twice - he must not hold two picks
    reloaded.record_pick(pool[50].player_key, pick=6)
    holders = [n for n, pk in reloaded.state.picks.items()
               if pk.player_key == pool[50].player_key]
    check(len(holders) == 1,
          f"{pool[50].name} holds picks {holders} - the draft is out of step "
          "with reality by one player")
    notes.append("duplicate entry: the earlier pick is released")

    tracker.reset()
    return notes


# --- entry point -------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", type=int, help="only this draft slot")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--db", help="read the board from this database")
    args = parser.parse_args(argv)

    cfg = Config.load("config.yaml")
    source = db.init_db(args.db or cfg.db_path)
    season = int(cfg.get("league.season", 2026))
    slots = {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 2, "DEF": 1}
    num_teams = int(cfg.get("league.num_teams", 12))

    # ignore_cleanup_errors: Windows will not unlink an open SQLite file, and a
    # simulation is not worth failing over a temp directory.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = snapshot(source, Path(tmp) / "sim.db")
        source.close()

        board = vorp.build_board(conn, season, slots, num_teams)
        if not board.players:
            print("No board could be built - is the database synced?")
            return 1
        print(f"board: {len(board.players)} players, season {season}\n")

        print("resilience:")
        for note in check_resilience(board, conn, slots, num_teams):
            print(f"  ok   {note}")
        print()

        slots_to_run = [args.slot] if args.slot else list(range(1, num_teams + 1))
        results, failures = [], []
        for slot in slots_to_run:
            for seed in range(args.seeds):
                try:
                    verbose = bool(args.slot) and seed == 0
                    if verbose:
                        print(f"draft from slot {slot}, seed {seed}:")
                    results.append(run_one(board, conn, slot, seed, slots,
                                           num_teams, verbose=verbose))
                except Failure as exc:
                    failures.append(f"slot {slot} seed {seed}: {exc}")

        print(f"\n{len(results)} drafts completed, {len(failures)} failed")
        for failure in failures:
            print(f"  FAIL  {failure}")

        if results:
            print("\n  slot  starters   vs best rival   vs median   rank")
            by_slot: dict[int, list] = {}
            for r in results:
                by_slot.setdefault(r["slot"], []).append(r)
            for slot in sorted(by_slot):
                rows = by_slot[slot]
                pts = sum(r["points"] for r in rows) / len(rows)
                best = sum(r["best_rival"] for r in rows) / len(rows)
                med = sum(r["median_rival"] for r in rows) / len(rows)
                rank = sum(r["rank"] for r in rows) / len(rows)
                print(f"  {slot:>4}  {pts:8.1f}   {pts - best:+13.1f}"
                      f"   {pts - med:+9.1f}   {rank:4.1f}")

            wins = sum(1 for r in results if r["rank"] == 1)
            top3 = sum(1 for r in results if r["rank"] <= 3)
            print(f"\n  best roster in {wins}/{len(results)} drafts, "
                  f"top three in {top3}/{len(results)}")

        conn.close()
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
