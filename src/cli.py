"""Command line entry point: `fcc <command>`.

Every command is headless-safe and returns a shell exit code suitable for cron
(spec 10): 0 success, 1 nothing-to-do-but-fine is still 0, 2 hard failure. A
source that is merely down degrades to cache and warns; it never crashes a job.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from src import db, league_bootstrap, projections as proj, scoring, vorp
from src.config import Config
from src.idmap import IdMapper
from src.notify import Notification, Notifier

log = logging.getLogger("fcc")

EXIT_OK = 0
EXIT_FAIL = 2


# --- shared context ----------------------------------------------------------

class Context:
    """Lazily assembles config, database and (optionally) the Yahoo client."""

    def __init__(self, config_path: str = "config.yaml", db_path: str | None = None):
        self.cfg = Config.load(config_path)
        self.conn = db.init_db(db_path or self.cfg.db_path)
        self.idmap = IdMapper(self.conn, self.cfg.get("paths.manual_id_overrides"))
        self._yahoo = None
        self._settings: dict[str, Any] | None = None

    @property
    def season(self) -> int:
        configured = self.cfg.get("league.season")
        if configured:
            return int(configured)
        from src.sources.sleeper import SleeperSource

        return SleeperSource(self.conn).current_season()

    @property
    def league_key(self) -> str:
        if self._yahoo is not None:
            try:
                return self.yahoo.league_key
            except Exception:
                pass
        return f"nfl.l.{self.cfg.get('league.league_id', league_bootstrap.LEAGUE_ID)}"

    @property
    def yahoo(self):
        if self._yahoo is None:
            from src.yahoo_client import YahooClient

            self._yahoo = YahooClient(self.cfg, self.conn)
        return self._yahoo

    def settings(self) -> dict[str, Any]:
        """League settings from the DB, bootstrapping if the API is not wired."""
        if self._settings is None:
            league_bootstrap.install(self.conn, self.league_key)
            row = self.conn.execute(
                "SELECT settings_json FROM league_settings WHERE league_key=?",
                (self.league_key,),
            ).fetchone()
            self._settings = json.loads(row["settings_json"]) if row else league_bootstrap.build_settings()
        return self._settings

    def scoring(self):
        return scoring.build_from_yahoo(self.settings())

    def starting_slots(self) -> dict[str, int]:
        raw = self.settings().get("roster_positions") or []
        slots: dict[str, int] = {}
        for entry in raw:
            rp = entry.get("roster_position", entry)
            pos = str(rp.get("position") or "")
            if pos and pos.upper() not in ("BN", "IR", "IR+", "NA"):
                slots[pos] = slots.get(pos, 0) + int(rp.get("count") or 0)
        return slots

    def num_teams(self) -> int:
        return int(self.settings().get("num_teams") or 12)

    def current_week(self) -> int:
        from src.sources.sleeper import SleeperSource

        state = SleeperSource(self.conn).state()
        week = int(state.get("week") or 1)
        return week if str(state.get("season_type")) == "regular" else 1

    def team_key(self) -> str | None:
        configured = self.cfg.get("league.my_team_id")
        return str(configured) if configured else None

    def notifier(self) -> Notifier:
        return Notifier(self.cfg, self.conn)

    def board(self, week: int = 0):
        return vorp.build_board(
            self.conn, self.season, self.starting_slots(), self.num_teams(),
            week=week, tier_gap_pct=float(self.cfg.get("draft.tier_gap_pct", 0.08)),
        )


# --- commands ----------------------------------------------------------------

def cmd_doctor(ctx: Context, args) -> int:
    """Report on every data source and on configuration completeness."""
    from src.sources.sleeper import SleeperSource
    from src.sources.sleeper_projections import SleeperProjections

    print("Fantasy Command Center - health check\n")
    print(f"  config      : {ctx.cfg.path}")
    print(f"  database    : {ctx.cfg.db_path}")
    print(f"  league key  : {ctx.league_key}")
    print(f"  season      : {ctx.season}")
    print(f"  roster slots: {ctx.starting_slots()}")
    rules = ctx.scoring()
    print(f"  scoring     : {len(rules.active_canonicals())} categories, PPR={rules.ppr_value}")

    from src.sources.espn import EspnSource
    from src.sources.fantasypros import FantasyProsSource
    from src.sources.nflverse import NflverseSource

    print("\n  sources:")
    sources = [
        SleeperSource(ctx.conn),
        SleeperProjections(ctx.conn),
        EspnSource(ctx.conn),
        FantasyProsSource(ctx.conn, ctx.cfg.get("sources.fantasypros.csv_dir")),
        NflverseSource(ctx.conn),
    ]
    for source in sources:
        try:
            health = source.health()
        except Exception as exc:
            health = {"source": source.name, "ok": False, "error": str(exc)}
        mark = "ok  " if health.get("ok") else "FAIL"
        detail = ", ".join(
            f"{k}={v}" for k, v in health.items() if k not in ("source", "ok")
        )
        print(f"    [{mark}] {health['source']:<14} {detail}")

    env_file = Path(ctx.cfg.get("paths.env_dir", ".")) / ".env"
    has_creds = env_file.exists() and "YAHOO_CONSUMER_KEY" in env_file.read_text(
        encoding="utf-8", errors="ignore"
    )
    print(f"\n  yahoo oauth : {'configured' if has_creds else 'NOT configured - run: fcc setup'}")

    counts = {
        table: ctx.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        for table in ("players", "projections", "projections_blended", "adp",
                      "injuries", "trending", "rosters", "free_agents", "draft_picks")
    }
    print("\n  stored rows :")
    for table, count in counts.items():
        print(f"    {table:22s} {count:>7,}")
    return EXIT_OK


def cmd_sync(ctx: Context, args) -> int:
    """Refresh every source that does not need Yahoo auth."""
    from src.sources.sleeper import SleeperSource
    from src.sources.sleeper_projections import SleeperProjections

    season = ctx.season
    rules = ctx.scoring()
    sleeper = SleeperSource(ctx.conn)
    sleeper_proj = SleeperProjections(ctx.conn)

    print(f"Syncing season {season}...")
    stats = sleeper.sync_players(ctx.idmap, force=args.force)
    print(f"  players     : {stats['seen']:,} seen, {stats['linked_yahoo']:,} carry a yahoo id")

    injuries = sleeper.sync_injuries(ctx.idmap)
    print(f"  injuries    : {injuries:,}")

    trending = sleeper.sync_trending(
        ctx.idmap,
        lookback_hours=int(ctx.cfg.get("sources.sleeper.trending_lookback_hours", 24)),
        limit=int(ctx.cfg.get("sources.sleeper.trending_limit", 50)),
    )
    print(f"  trending    : {trending}")

    season_stats = sleeper_proj.sync(ctx.idmap, rules, season, force=args.force)
    print(f"  season proj : {season_stats['stored']:,} stored, {season_stats['unmatched']} unmatched")

    # Defenses need rebuilding from weekly lines; see sync_defense_season.
    def_stats = sleeper_proj.sync_defense_season(ctx.idmap, rules, season, force=args.force)
    print(f"  defense     : {def_stats['defenses']} rebuilt from {def_stats['weeks']} weekly lines")

    adp_rows = sleeper_proj.sync_adp(ctx.idmap, season, ppr_value=rules.ppr_value)
    print(f"  adp         : {adp_rows:,} (sleeper)")

    if ctx.cfg.get("sources.espn.enabled", True):
        from src.sources.espn import EspnSource

        espn = EspnSource(ctx.conn)
        try:
            espn_stats = espn.sync(ctx.idmap, rules, season, force=args.force)
            print(f"  espn proj   : {espn_stats['stored']:,} stored, "
                  f"{espn_stats['unmatched']} unmatched")
        except Exception as exc:
            log.warning("ESPN sync skipped: %s", exc)
            print(f"  espn proj   : unavailable ({exc})")

    if ctx.cfg.get("sources.fantasypros.enabled", True):
        from src.sources.fantasypros import FantasyProsSource

        fp = FantasyProsSource(ctx.conn, ctx.cfg.get("sources.fantasypros.csv_dir"))
        try:
            fp_stats = fp.sync(ctx.idmap, force=args.force)
            print(f"  ecr         : {fp_stats['stored']:,} stored, "
                  f"{fp_stats['unmatched']} unmatched, {fp_stats['byes']:,} byes set")
        except Exception as exc:
            log.warning("FantasyPros sync skipped: %s", exc)
            print(f"  ecr         : unavailable ({exc})")

    if ctx.cfg.get("sources.nflverse.enabled", True):
        from src.sources.nflverse import NflverseSource

        nv = NflverseSource(ctx.conn)
        try:
            byes = nv.sync_bye_weeks(season, force=args.force)
            print(f"  byes        : {byes:,} players updated from the NFL schedule")
        except Exception as exc:
            log.warning("nflverse sync skipped: %s", exc)
            print(f"  byes        : unavailable ({exc})")

    week = args.week if args.week is not None else ctx.current_week()
    if week:
        weekly = sleeper_proj.sync(ctx.idmap, rules, season, week=week, force=args.force)
        print(f"  week {week} proj: {weekly['stored']:,} stored")

    blended = proj.blend_all(
        ctx.conn, season, 0,
        weights=ctx.cfg.get("projections.weights"),
        band_sd=float(ctx.cfg.get("projections.uncertainty_band_sd", 1.0)),
    )
    print(f"  blended     : {blended:,} season projections")
    if week:
        proj.blend_all(ctx.conn, season, week, weights=ctx.cfg.get("projections.weights"))
    return EXIT_OK


def cmd_rank(ctx: Context, args) -> int:
    """Print the draft board (spec phase 2: `fcc rank`)."""
    board = ctx.board()
    if not board.players:
        print("No projections stored yet. Run: fcc sync")
        return EXIT_OK

    if args.position:
        players = [p for p in board.players if p.position == args.position.upper()]
    else:
        players = board.players

    print(f"\nReplacement levels ({ctx.num_teams()} teams, slots {ctx.starting_slots()}):")
    for pos, level in sorted(board.replacement.items()):
        print(f"  {pos:4s} {pos}{level.rank:<3d} = {level.points:7.1f}  "
              f"(flex share {level.flex_share:.0f})")

    print(f"\nScarcity (pts lost per pick waited):")
    for pos, value in sorted(board.scarcity.items(), key=lambda kv: -kv[1]):
        print(f"  {pos:4s} {value:6.2f}")

    print(f"\n{'#':>4} {'PLAYER':<24} {'POS':<6} {'TIER':<5} {'TEAM':<5} "
          f"{'PROJ':>7} {'VORP':>7} {'ADP':>6} {'BYE':>4}")
    print("-" * 84)
    for p in players[: args.limit]:
        adp = f"{p.adp:6.1f}" if p.adp else "     -"
        bye = f"{p.bye_week:4d}" if p.bye_week else "   -"
        flag = " *" if p.injury_status else ""
        print(
            f"{p.overall_rank:>4} {p.name[:24]:<24} {p.position}{p.position_rank:<5} "
            f"T{p.tier:<4} {p.team:<5} {p.points:>7.1f} {p.vorp:>7.1f} {adp} {bye}{flag}"
        )
    return EXIT_OK


def cmd_mockdraft(ctx: Context, args) -> int:
    """Rehearse a draft, or run the strategy comparison (spec 10.3)."""
    from src.draft.mockdraft import compare_strategies, simulate_draft

    board = ctx.board()
    slots = ctx.starting_slots()
    teams = ctx.num_teams()
    rounds = args.rounds or _default_rounds(ctx)
    defer = tuple(ctx.cfg.get("draft.defer_positions", ["K", "DEF"]))
    defer_round = int(ctx.cfg.get("draft.defer_until_round", 12))

    if args.compare:
        print(f"Running {args.n} paired drafts ({teams} teams, {rounds} rounds)...")
        result = compare_strategies(
            board, slots, n=args.n, num_teams=teams, rounds=rounds,
            my_slot=args.slot, defer_positions=defer, defer_until_round=defer_round,
        )
        print(f"\n  recommender mean : {result.recommender_mean}")
        print(f"  naive ADP mean   : {result.baseline_mean}")
        print(f"  mean edge        : {result.mean_edge:+.2f}")
        print(f"  win rate         : {result.win_rate:.0%}")
        print(f"  mean rank        : {result.recommender_rank} vs {result.baseline_rank}")
        print(f"\n  beats baseline   : {result.beats_baseline}")
        return EXIT_OK if result.beats_baseline else EXIT_FAIL

    slot = args.slot or 1
    result = simulate_draft(
        board, slots, teams, rounds, my_slot=slot, seed=args.seed,
        defer_positions=defer, defer_until_round=defer_round,
    )
    print(f"\nMock draft from slot {slot} ({teams} teams, {rounds} rounds)")
    print(f"  your projected starting lineup : {result.my_points:.1f}")
    print(f"  league average                 : {result.opponent_mean:.1f}")
    print(f"  finish                         : {result.my_rank} of {teams}\n")
    for i, p in enumerate(result.my_roster, 1):
        print(f"  R{i:<3d} {p.name[:26]:<26} {p.position}{p.position_rank:<4} {p.points:>7.1f}")
    return EXIT_OK


def cmd_draft(ctx: Context, args) -> int:
    """Live draft assistant (spec 5.2)."""
    from src.draft.live import DraftTracker
    from src.draft.recommender import DraftRecommender, RosterState
    from src.draft.survival import DraftPosition

    board = ctx.board()
    slots = ctx.starting_slots()
    teams = ctx.num_teams()
    rounds = args.rounds or _default_rounds(ctx)
    my_slot = args.slot or int(ctx.cfg.get("draft.draft_slot") or 1)

    yahoo = None
    if not args.offline:
        try:
            yahoo = ctx.yahoo
        except Exception as exc:
            print(f"Yahoo unavailable ({exc}); running in manual mode.")

    tracker = DraftTracker(ctx.conn, ctx.league_key, teams, rounds, yahoo)
    position = DraftPosition(num_teams=teams, draft_slot=my_slot, rounds=rounds)
    recommender = DraftRecommender(
        board, position,
        need_weight=float(ctx.cfg.get("draft.need_weight", 0.35)),
        defer_positions=ctx.cfg.get("draft.defer_positions", ["K", "DEF"]),
        defer_until_round=int(ctx.cfg.get("draft.defer_until_round", 12)),
        bye_stack_threshold=int(ctx.cfg.get("draft.bye_stack_warn_threshold", 3)),
        te_flex_credit=int(ctx.cfg.get("draft.te_flex_credit", 0)),
    )

    if not args.offline:
        tracker.sync_from_yahoo()
    _print_recommendations(tracker, recommender, board, slots, my_slot, position)
    return EXIT_OK


def _print_recommendations(tracker, recommender, board, slots, my_slot, position) -> None:
    from src.draft.recommender import RosterState

    drafted = tracker.state.drafted_keys
    my_keys = set(tracker.state.roster_of(str(my_slot)))
    my_players = [p for p in board.players if p.player_key in my_keys]
    roster = RosterState(starting_slots=slots, players=my_players)
    current = tracker.state.next_pick

    print(f"\nPick {current} (round {position.current_round(current)}), "
          f"your slot {my_slot}. {len(drafted)} players off the board.")
    next_pick = position.next_pick_after(current)
    if next_pick:
        print(f"Your next pick after this: {next_pick} "
              f"({next_pick - current} picks away)")

    print(f"\n{'#':>3} {'PLAYER':<24} {'POS':<6} {'SCORE':>7} {'VORP':>7} {'SURV':>6}")
    print("-" * 70)
    for i, rec in enumerate(recommender.recommend(drafted, roster, current, top_n=10), 1):
        p = rec.player
        survival = f"{rec.survival:5.0%}" if rec.survival is not None else "    -"
        print(f"{i:>3} {p.name[:24]:<24} {p.position}{p.position_rank:<5} "
              f"{rec.score:>7.1f} {rec.vorp:>7.1f} {survival}")
        for reason in rec.reasons[:2]:
            print(f"      + {reason}")
        for warning in rec.warnings[:2]:
            print(f"      ! {warning}")

    unfilled = roster.unfilled()
    if unfilled:
        gaps = ", ".join(f"{k} {v:.1f}" for k, v in sorted(unfilled.items()) if v > 0)
        print(f"\nStarting slots still open: {gaps or 'none'}")


def cmd_job(ctx: Context, args) -> int:
    """Run one season job and notify (spec 6)."""
    from src.season import byes, injuries, lineup, recap, trades, waivers

    season = ctx.season
    week = args.week if args.week is not None else ctx.current_week()
    slots = ctx.starting_slots()
    team_key = ctx.team_key()
    notifier = ctx.notifier()
    notification: Notification | None = None

    if args.job == "injuries":
        report = injuries.run(ctx.conn, ctx.league_key, team_key, season, week)
        if report.first_run:
            print("Injury baseline established; changes will be reported from the next run.")
        else:
            print(f"{len(report.changes)} relevant status change(s) out of {report.checked} tracked.")
        notification = injuries.to_notification(report, week, season)

    elif args.job == "lineup":
        if not team_key:
            print("league.my_team_id is not set; run `fcc whoami` once Yahoo auth is configured.")
            return EXIT_OK
        report = lineup.run(
            ctx.conn, ctx.league_key, team_key, season, week, slots,
            risk_mode=str(ctx.cfg.get("season.risk_mode", "auto")),
            min_gap=float(ctx.cfg.get("season.lineup_swap_min_gap", 1.5)),
        )
        print(f"Optimal {report.optimal_points:.1f} vs current {report.current_points:.1f} "
              f"(+{report.gain:.1f}); {len(report.swaps)} change(s).")
        notification = lineup.to_notification(report, season)

    elif args.job == "waivers":
        if not team_key:
            print("league.my_team_id is not set; skipping.")
            return EXIT_OK
        settings = ctx.settings()
        report = waivers.run(
            ctx.conn, ctx.league_key, team_key, season, week,
            uses_faab=str(settings.get("uses_faab", "1")) in ("1", "true", "True"),
            budget_left=int(args.budget if args.budget is not None
                            else settings.get("faab_budget", 100)),
            value_margin=float(ctx.cfg.get("season.waiver_value_margin", 8.0)),
        )
        print(f"{len(report.claims)} claim(s), {len(report.stashes)} stash(es), "
              f"{len(report.handcuffs)} open handcuff(s).")
        notification = waivers.to_notification(report, season)

    elif args.job == "byes":
        if not team_key:
            print("league.my_team_id is not set; skipping.")
            return EXIT_OK
        report = byes.run(ctx.conn, ctx.league_key, team_key, season, week, slots,
                          playoff_weeks=tuple(ctx.cfg.get("season.playoff_weeks", [15, 16, 17])))
        print(f"{len(report.problem_weeks)} problem week(s) in the next 4.")
        notification = byes.to_notification(report, season)

    elif args.job == "recap":
        if not team_key:
            print("league.my_team_id is not set; skipping.")
            return EXIT_OK
        report = recap.run(ctx.conn, ctx.league_key, team_key, season, max(1, week - 1), slots)
        print(f"Week {report.week}: {report.actual_points:.1f} scored, "
              f"{report.points_left_on_bench:.1f} left on bench.")
        notification = recap.to_notification(report, season)

    elif args.job == "trades":
        if not team_key:
            print("league.my_team_id is not set; skipping.")
            return EXIT_OK
        ideas = trades.run(ctx.conn, ctx.league_key, team_key, season, week, slots)
        print(f"{len(ideas)} mutually-beneficial trade idea(s).")
        notification = trades.to_notification(ideas, season, week)

    if notification is None:
        print("Nothing actionable; no notification sent.")
        return EXIT_OK

    if args.dry_run:
        print("\n--- notification preview ---")
        print(notification.text())
        return EXIT_OK

    result = notifier.send(notification)
    print(f"Notification: {result}")
    return EXIT_OK


def cmd_daily(ctx: Context, args) -> int:
    """Sync then run every job that is due today (spec 11.3: idempotent)."""
    import datetime as dt

    weekday = dt.datetime.now().strftime("%a").upper()[:3]
    schedule = {
        "TUE": ["waivers", "injuries"],
        "WED": ["byes", "injuries"],
        "THU": ["lineup", "injuries"],
        "SUN": ["lineup", "injuries"],
        "MON": ["recap", "trades", "injuries"],
    }
    jobs = schedule.get(weekday, ["injuries"])

    class _A:
        force = False
        week = None

    cmd_sync(ctx, _A())
    failures = 0
    for job in jobs:
        print(f"\n=== {job} ===")

        class _J:
            pass

        job_args = _J()
        job_args.job = job
        job_args.week = args.week
        job_args.dry_run = args.dry_run
        job_args.budget = None
        try:
            cmd_job(ctx, job_args)
        except Exception:
            failures += 1
            traceback.print_exc()
    return EXIT_FAIL if failures else EXIT_OK


def cmd_setup(ctx_or_none, args) -> int:
    """Interactive one-time Yahoo credential setup (spec 11)."""
    print(
        "\nYahoo API setup\n"
        "---------------\n"
        "1. Open https://developer.yahoo.com/apps/create/\n"
        "2. Application Type: Installed Application\n"
        "3. Redirect URI: https://localhost:8080\n"
        "4. API Permissions: Fantasy Sports -> Read (or Read/Write for auto-lineup later)\n"
        "5. Create the app, then copy the Client ID (consumer key) and Client Secret.\n"
    )
    env_path = Path(args.env_dir) / ".env"
    key = input("Paste your Yahoo Client ID (consumer key): ").strip()
    secret = input("Paste your Yahoo Client Secret: ").strip()
    if not key or not secret:
        print("Both values are required; nothing was written.")
        return EXIT_FAIL

    existing = ""
    if env_path.exists():
        existing = "\n".join(
            line for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("YAHOO_CONSUMER_KEY=", "YAHOO_CONSUMER_SECRET="))
        )
    env_path.write_text(
        (existing.rstrip() + "\n" if existing.strip() else "")
        + f"YAHOO_CONSUMER_KEY={key}\nYAHOO_CONSUMER_SECRET={secret}\n",
        encoding="utf-8",
    )
    print(f"\nWrote credentials to {env_path.resolve()}")
    print("Next: run `fcc sync-settings` - a browser window will open once for consent.")
    return EXIT_OK


def cmd_sync_settings(ctx: Context, args) -> int:
    """Pull authoritative league settings from Yahoo, replacing the bootstrap."""
    yahoo = ctx.yahoo
    settings = yahoo.fetch_league_settings(force=True)
    rules = scoring.build_from_yahoo(settings)
    print(f"Fetched settings for {yahoo.league_key}")
    print(f"  teams        : {yahoo.num_teams()}")
    print(f"  roster slots : {yahoo.starting_slots()}")
    print(f"  bench        : {yahoo.bench_size()}")
    print(f"  waivers      : {yahoo.waiver_config()}")
    print(f"  scoring      : {len(rules.active_canonicals())} categories, PPR={rules.ppr_value}")

    team_id = yahoo.my_team_id()
    if team_id:
        ctx.cfg.set("league.my_team_id", team_id)
        ctx.cfg.save()
        print(f"  your team id : {team_id} (saved to config.yaml)")
    return EXIT_OK


def cmd_verify_settings(ctx: Context, args) -> int:
    """Diff the captured bootstrap against live Yahoo settings."""
    live = scoring.build_from_yahoo(ctx.yahoo.fetch_league_settings(force=True))
    boot = scoring.build_from_yahoo(league_bootstrap.build_settings())

    live_mods = {c.display_name: c.modifier for c in live.categories if c.enabled}
    boot_mods = {c.display_name: c.modifier for c in boot.categories if c.enabled}
    differences = []
    for name in sorted(set(live_mods) | set(boot_mods)):
        a, b = boot_mods.get(name), live_mods.get(name)
        if a != b:
            differences.append(f"  {name:<22} bootstrap={a}  yahoo={b}")
    if differences:
        print("Differences found (Yahoo is authoritative):")
        print("\n".join(differences))
        return EXIT_FAIL
    print("Bootstrap scoring matches Yahoo exactly.")
    return EXIT_OK


def cmd_dashboard(ctx: Context, args) -> int:
    import subprocess

    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", "dashboard.py",
         "--server.headless", "true"]
    )


def _default_rounds(ctx: Context) -> int:
    """Starters plus bench: the number of picks each team actually makes."""
    raw = ctx.settings().get("roster_positions") or []
    total = 0
    for entry in raw:
        rp = entry.get("roster_position", entry)
        pos = str(rp.get("position") or "").upper()
        if pos in ("IR", "IR+", "NA"):
            continue
        total += int(rp.get("count") or 0)
    return total or 15


# --- argument parsing --------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fcc", description="Fantasy Command Center")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check sources, config and stored data")

    p_setup = sub.add_parser("setup", help="one-time Yahoo credential setup")
    p_setup.add_argument("--env-dir", default=".")

    p_sync = sub.add_parser("sync", help="refresh players, projections, ADP, injuries")
    p_sync.add_argument("--force", action="store_true", help="bypass caches")
    p_sync.add_argument("--week", type=int)

    sub.add_parser("sync-settings", help="pull league settings from Yahoo")
    sub.add_parser("verify-settings", help="diff bootstrap settings against Yahoo")

    p_rank = sub.add_parser("rank", help="print the VORP draft board")
    p_rank.add_argument("--limit", type=int, default=50)
    p_rank.add_argument("--position")

    p_draft = sub.add_parser("draft", help="live draft assistant")
    p_draft.add_argument("--slot", type=int)
    p_draft.add_argument("--rounds", type=int)
    p_draft.add_argument("--offline", action="store_true", help="manual mode, no Yahoo polling")

    p_mock = sub.add_parser("mockdraft", help="rehearse a draft or benchmark the strategy")
    p_mock.add_argument("--slot", type=int)
    p_mock.add_argument("--rounds", type=int)
    p_mock.add_argument("--seed", type=int, default=42)
    p_mock.add_argument("--compare", action="store_true", help="run the acceptance benchmark")
    p_mock.add_argument("-n", type=int, default=100)

    for job in ("injuries", "lineup", "waivers", "byes", "recap", "trades"):
        p_job = sub.add_parser(job, help=f"run the {job} job")
        p_job.add_argument("--week", type=int)
        p_job.add_argument("--dry-run", action="store_true")
        p_job.add_argument("--budget", type=int, help="FAAB budget remaining")
        p_job.set_defaults(job=job)

    p_daily = sub.add_parser("daily", help="sync and run everything due today")
    p_daily.add_argument("--week", type=int)
    p_daily.add_argument("--dry-run", action="store_true")

    sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "setup":
        return cmd_setup(None, args)

    try:
        ctx = Context(args.config, args.db)
    except Exception as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    handlers = {
        "doctor": cmd_doctor,
        "sync": cmd_sync,
        "sync-settings": cmd_sync_settings,
        "verify-settings": cmd_verify_settings,
        "rank": cmd_rank,
        "draft": cmd_draft,
        "mockdraft": cmd_mockdraft,
        "daily": cmd_daily,
        "dashboard": cmd_dashboard,
    }
    handler = handlers.get(args.command, cmd_job)
    try:
        return handler(ctx, args)
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception:
        traceback.print_exc()
        return EXIT_FAIL
    finally:
        ctx.conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
