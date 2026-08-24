"""Command line entry point: `fcc <command>`.

Every command is headless-safe and returns a shell exit code suitable for cron
(spec 10): 0 success, 1 nothing-to-do-but-fine is still 0, 2 hard failure. A
source that is merely down degrades to cache and warns; it never crashes a job.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
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
        # `--db` names a file, so it means SQLite - even when .env or Streamlit
        # secrets define DATABASE_URL. Without this the flag looked like it
        # worked and quietly operated on the hosted league database instead.
        self.conn = db.init_db(
            db_path or self.cfg.db_path, force_sqlite=db_path is not None
        )
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

    def yahoo_configured(self) -> bool:
        """Whether Yahoo credentials are present, without authenticating.

        Checked in two places because the two deployments differ: locally the
        credentials live in a .env file, while on Streamlit Cloud and GitHub
        Actions they arrive as environment variables and no .env exists. Looking
        only at the file made every hosted run silently skip the league sync.

        A real assignment with a real value, not a substring match: the file
        ships with `# YAHOO_CONSUMER_KEY=...` commented out as a template, and
        a naive `in` test reported that placeholder as configured - which would
        have sent every scheduled run off to authenticate with nothing.
        """
        if (os.environ.get("YAHOO_CONSUMER_KEY") or "").strip():
            return True
        env_file = Path(self.cfg.get("paths.env_dir", ".")) / ".env"
        if not env_file.exists():
            return False
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "YAHOO_CONSUMER_KEY" and value.strip().strip("\"'"):
                return True
        return False

    def team_key(self) -> str | None:
        configured = self.cfg.get("league.my_team_id")
        return str(configured) if configured else None

    def notifier(self) -> Notifier:
        return Notifier(self.cfg, self.conn)

    def board(self, week: int = 0):
        return vorp.build_board(
            self.conn, self.season, self.starting_slots(), self.num_teams(),
            week=week, tier_gap_pct=float(self.cfg.get("draft.tier_gap_pct", 0.08)),
            prior_strength=float(
                self.cfg.get("draft.prior_regression_strength", 0.0)
            ),
        )


# --- commands ----------------------------------------------------------------

def cmd_doctor(ctx: Context, args) -> int:
    """Report on every data source and on configuration completeness."""
    from src.sources.sleeper import SleeperSource
    from src.sources.sleeper_projections import SleeperProjections

    print("Fantasy Command Center - health check\n")
    print(f"  config      : {ctx.cfg.path}")
    print(f"  database    : {db.describe_backend()}")
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

    has_creds = ctx.yahoo_configured()
    print(f"\n  yahoo oauth : {'configured' if has_creds else 'NOT configured - run: fcc setup'}")

    counts = {
        table: ctx.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        for table in ("players", "projections", "projections_blended", "adp",
                      "injuries", "trending", "rosters", "free_agents", "draft_picks",
                      "transactions", "team_budgets")
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

    # The league's own state, which only Yahoo has. Skipped rather than failed
    # when credentials are absent, so `fcc sync` keeps working before access is
    # granted - which is exactly the state this project has been in.
    if ctx.yahoo_configured():
        sync_yahoo_league(ctx, season, week or 1, force=args.force)
    else:
        print("  yahoo       : skipped (no credentials; run: fcc setup)")
    return EXIT_OK


def sync_yahoo_league(ctx: Context, season: int, week: int, force: bool = False) -> dict:
    """Pull the league's own state: teams, rosters, free agents, transactions.

    Every season job reads these four tables and nothing else filled them, so
    until this ran the whole of season mode was querying empty tables and
    reporting "no claims" rather than failing - the quietest possible way for a
    feature to be broken.

    Each leg is independent: a failure in one is reported and the rest still
    run, because a waiver run with stale free agents is far better than none.
    """
    yahoo = ctx.yahoo
    out = {"teams": 0, "rosters": 0, "free_agents": 0, "transactions": 0}

    try:
        teams = yahoo.fetch_teams(force=force)
        out["teams"] = yahoo.store_teams(teams, season)
        print(f"  teams       : {out['teams']}")
    except Exception as exc:
        log.warning("Yahoo teams sync failed: %s", exc)
        print(f"  teams       : unavailable ({exc})")
        teams = []

    for team in teams:
        team_id = team.get("team_id")
        if team_id in (None, ""):
            continue
        try:
            players = yahoo.fetch_roster(int(team_id), week, force=force)
            out["rosters"] += yahoo.store_roster(
                int(team_id), week, players, team.get("name")
            )
        except Exception as exc:
            log.warning("Roster sync failed for team %s: %s", team_id, exc)
    if teams:
        print(f"  rosters     : {out['rosters']:,} slots across {len(teams)} teams")

    try:
        free_agents = yahoo.fetch_free_agents(
            count=int(ctx.cfg.get("sources.yahoo.free_agent_count", 200)), force=force
        )
        out["free_agents"] = yahoo.store_free_agents(free_agents, week)
        print(f"  free agents : {out['free_agents']:,}")
    except Exception as exc:
        log.warning("Free agent sync failed: %s", exc)
        print(f"  free agents : unavailable ({exc})")

    try:
        txns = yahoo.fetch_transactions(force=force)
        out["transactions"] = yahoo.store_transactions(txns)
        print(f"  transactions: {out['transactions']:,} (FAAB bid history)")
    except Exception as exc:
        log.warning("Transaction sync failed: %s", exc)
        print(f"  transactions: unavailable ({exc})")

    return out


def cmd_sync_league(ctx: Context, args) -> int:
    """`fcc sync-league` - the Yahoo half of the sync on its own."""
    if not ctx.yahoo_configured():
        print("Yahoo credentials are not configured; run: fcc setup")
        return EXIT_FAIL
    week = args.week if args.week is not None else ctx.current_week()
    print(f"Syncing league state for week {week}...")
    sync_yahoo_league(ctx, ctx.season, week, force=args.force)
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
        # ^ ran hot last season and is priced on it; v ran cold and is not.
        if p.prior_verdict == "inflated":
            flag += " ^"
        elif p.prior_verdict == "deflated":
            flag += " v"
        print(
            f"{p.overall_rank:>4} {p.name[:24]:<24} {p.position}{p.position_rank:<5} "
            f"T{p.tier:<4} {p.team:<5} {p.points:>7.1f} {p.vorp:>7.1f} {adp} {bye}{flag}"
        )
    flagged = sum(1 for p in players if p.prior_verdict)
    if flagged:
        print("")
        print("  * injury   ^ last season ran hot   v last season ran cold "
              f"({flagged} of {len(players)} flagged)")
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
            # --seed was accepted and then dropped here, so every run of the
            # comparison returned byte-identical numbers however it was invoked.
            seed=args.seed if args.seed is not None else 1234,
            bye_stack_threshold=int(ctx.cfg.get("draft.bye_stack_warn_threshold", 3)),
            te_flex_credit=int(ctx.cfg.get("draft.te_flex_credit", 0)),
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
        bye_stack_threshold=int(ctx.cfg.get("draft.bye_stack_warn_threshold", 3)),
        te_flex_credit=int(ctx.cfg.get("draft.te_flex_credit", 0)),
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


def _my_faab_left(ctx: Context, season: int, settings: dict) -> int:
    """Your remaining FAAB, falling back to the full budget before any sync.

    Recommending bids against the season-opening budget in week 11 is not a
    small error - it is the difference between a bid you can make and one you
    cannot.
    """
    default = int(settings.get("faab_budget") or 100)
    if not ctx.conn.table_exists("team_budgets"):
        return default
    row = ctx.conn.fetchone(
        "SELECT faab_balance FROM team_budgets "
        "WHERE league_key=? AND season=? AND team_key=?",
        (ctx.league_key, season, str(ctx.team_key() or "")),
    )
    if row and row["faab_balance"] is not None:
        return int(row["faab_balance"])
    return default


def cmd_job(ctx: Context, args) -> int:
    """Run one season job and notify (spec 6)."""
    from src.season import byes, injuries, lineup, recap, reminders, trades, waivers

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
        if not report.has_data:
            print(f"No week {week} projections for your roster "
                  f"({report.roster_size} player(s) rostered, "
                  f"{report.projected} projected) - nothing to compare.")
        else:
            print(f"Optimal {report.optimal_points:.1f} vs current "
                  f"{report.current_points:.1f} "
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
                            else _my_faab_left(ctx, season, settings)),
            value_margin=float(ctx.cfg.get("season.waiver_value_margin", 25.0)),
            starting_slots=slots,
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
        if not report.has_data:
            print(f"No week {report.week} scores stored "
                  f"({report.roster_size} player(s) rostered) - no recap.")
        else:
            print(f"Week {report.week}: {report.actual_points:.1f} scored, "
                  f"{report.points_left_on_bench:.1f} left on bench.")
        notification = recap.to_notification(report, season)

    elif args.job == "reminders":
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(str(ctx.cfg.get("schedule.timezone", "America/New_York")))
        settings = ctx.settings()
        gaps: list[str] = []
        if team_key:
            try:
                bye_report = byes.run(
                    ctx.conn, ctx.league_key, team_key, season, week, slots, horizon=1
                )
                gaps = [
                    f"Week {w.week}: cannot fill {', '.join(w.empty_slots)}"
                    for w in bye_report.problem_weeks
                ]
            except Exception as exc:
                log.warning("Could not check roster gaps for reminders: %s", exc)

        found = reminders.build(
            settings, datetime.now(tz), week=week, roster_gaps=gaps,
            faab_left=args.budget,
        )
        print(f"{len(found)} reminder(s) apply right now.")
        notification = reminders.to_notification(found, season, week)

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
        "TUE": ["waivers", "injuries", "reminders"],
        "WED": ["byes", "injuries"],
        "THU": ["lineup", "injuries", "reminders"],
        "SUN": ["lineup", "injuries", "reminders"],
        "MON": ["recap", "trades", "injuries"],
    }
    jobs = schedule.get(weekday, ["injuries", "reminders"])

    class _A:
        force = False
        week = None
        season = None

    cmd_sync(ctx, _A())

    # Opportunity data. Nothing else populates player_week_usage, so without
    # this every feature built on it - prior-season regression flags, the
    # expected-points signals, measured volatility - stays permanently dark.
    try:
        cmd_sync_usage(ctx, _A())
    except Exception as exc:
        log.warning("Usage sync skipped: %s", exc)
        print(f"  usage       : unavailable ({exc})")
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


def cmd_migrate(ctx: Context, args) -> int:
    """Copy the local SQLite database into the configured Postgres."""
    from src import migrate as migrate_mod
    from src.storage import connect_postgres, connect_sqlite, database_url

    target_url = args.to or database_url()
    if not target_url:
        print("No target set. Put DATABASE_URL in .env, or pass --to <postgres url>.")
        return EXIT_FAIL

    source = connect_sqlite(args.source or ctx.cfg.db_path)
    target = connect_postgres(target_url)
    target.executescript(db.schema_for("postgres"))

    print(f"Copying {source.url} -> postgres")
    if args.dry_run:
        print("(dry run: reading only, nothing written)")

    def show(table, stats):
        if stats["read"]:
            print(f"  {table:24s} read {stats['read']:>7,}  written {stats['written']:>7,}")

    try:
        results = migrate_mod.migrate(source, target, dry_run=args.dry_run, progress=show)
    finally:
        source.close()
        target.close()

    total_read = sum(s["read"] for s in results.values())
    total_written = sum(s["written"] for s in results.values())
    print(f"\n  total: read {total_read:,}, written {total_written:,}")
    return EXIT_OK


def cmd_sync_usage(ctx: Context, args) -> int:
    """Pull opportunity, expected points, practice reports and depth charts."""
    from src.sources.context import ContextSource
    from src.sources.usage import UsageSource

    season = args.season or ctx.season
    usage = UsageSource(ctx.conn)
    rules = ctx.scoring()

    print(f"Syncing usage and context for {season}...")
    stats = usage.sync_usage(ctx.idmap, rules, season, force=args.force)
    print(f"  usage       : {stats['stored']:,} player-weeks "
          f"({stats['unmatched']} unmatched)")

    practice = usage.sync_practice_reports(ctx.idmap, season)
    print(f"  practice    : {practice:,} official injury-report rows")

    depth = usage.sync_depth_charts(ctx.idmap, season)
    print(f"  depth charts: {depth:,} slots")

    week = args.week if args.week is not None else ctx.current_week()
    context = ContextSource(ctx.conn)
    odds = context.sync_odds(season, week, force=args.force)
    if odds:
        weather = context.sync_weather(season, week, force=args.force)
        print(f"  betting mkt : {odds} team-weeks, weather for {weather} venues")
    else:
        print("  betting mkt : skipped (set ODDS_API_KEY in .env to enable)")
    return EXIT_OK


def cmd_regression(ctx: Context, args) -> int:
    """Buy-low and sell-high candidates from expected-points regression."""
    from src.analytics import regression

    season = args.season or ctx.season
    signals = regression.scan(
        ctx.conn, season, through_week=args.week, window=args.window
    )
    if not signals:
        print(f"No actionable signals for {season}.")
        print("This needs several weeks of played games; run `fcc sync-usage` first.")
        return EXIT_OK

    for verdict, label in (
        ("buy", "BUY LOW - underperforming their usage"),
        ("sell", "SELL HIGH - outscoring their usage"),
    ):
        group = [s for s in signals if s.verdict == verdict][: args.limit]
        if not group:
            continue
        print("")
        print(label)
        print("-" * len(label))
        for s in group:
            print(f"  {s.name:<22} {s.position} {s.team:<4} "
                  f"actual {s.points_actual:5.1f}  expected {s.points_expected:5.1f}  "
                  f"gap {s.residual:+5.1f}/gm  ({s.games}g, {s.confidence:.0%} conf)")
            for reason in s.reasons:
                print(f"      {reason}")
    return EXIT_OK


def cmd_accuracy(ctx: Context, args) -> int:
    """Which projection source is most accurate for this league scoring."""
    from src.analytics import accuracy

    season = args.season or ctx.season
    results = accuracy.score_sources(ctx.conn, season, through_week=args.week)
    for line in accuracy.report(results):
        print(line)

    if results:
        weights = accuracy.derive_weights(results)
        print("Earned blend weights (shrunk toward equal while evidence is thin):")
        for source, w in sorted(weights.items(), key=lambda kv: -kv[1]):
            print(f"  {source:<14} {w:.3f}")
        accuracy.store(ctx.conn, results, season, args.week or ctx.current_week())
    return EXIT_OK


def cmd_startsit(ctx: Context, args) -> int:
    """The lineup with the best chance of beating a given opponent total."""
    from src.analytics.distributions import (
        PlayerForecast, optimise, simulate, swap_impact,
    )

    season = ctx.season
    week = args.week if args.week is not None else ctx.current_week()
    team_key = ctx.team_key()
    if not team_key:
        print("league.my_team_id is not set; run `fcc sync-settings` once Yahoo is wired.")
        return EXIT_OK

    rows = ctx.conn.fetchall(
        """
        SELECT r.player_key, p.full_name, p.position, p.team,
               COALESCE(b.points, j.points, 0) AS mean,
               COALESCE(b.stdev, 0)            AS sd
        FROM rosters r
        JOIN players p USING(player_key)
        LEFT JOIN projections_blended b
               ON b.player_key=r.player_key AND b.season=? AND b.week=?
        LEFT JOIN projections j
               ON j.player_key=r.player_key AND j.season=? AND j.week=?
              AND j.source='sleeper'
        WHERE r.league_key=? AND r.team_key=? AND r.week=?
        """,
        (season, week, season, week, ctx.league_key, team_key, week),
    )
    if not rows:
        print(f"No roster stored for week {week}. Yahoo sync is needed for this.")
        return EXIT_OK

    # A roster with no projections produces a lineup of zeroes and a confident
    # "0% to win", which reads as a verdict rather than as missing data. The
    # same trap the lineup job fell into.
    projected = sum(1 for r in rows if float(r["mean"] or 0) > 0)
    if not projected:
        print(f"No week {week} projections for your roster "
              f"({len(rows)} player(s) rostered). Run `fcc sync --week {week}`.")
        return EXIT_OK

    roster = [
        PlayerForecast(
            r["player_key"], r["full_name"], r["position"], r["team"] or "",
            float(r["mean"] or 0.0),
            # A missing spread would imply certainty; fall back to a positional
            # rule of thumb rather than zero.
            float(r["sd"]) if r["sd"] else max(2.0, float(r["mean"] or 0) * 0.45),
        )
        for r in rows
    ]
    slots = ctx.starting_slots()
    outcome = optimise(roster, slots, args.opponent, args.opponent_sd)

    print(f"Week {week} vs a projected {args.opponent:.0f} +/- {args.opponent_sd:.0f}")
    print("")
    print(f"  {outcome.describe()}")
    print("")
    for slot in sorted(outcome.players, key=lambda p: -p.mean):
        print(f"    {slot.name:<22} {slot.position:<4} "
              f"{slot.mean:5.1f} +/- {slot.sd:4.1f}")

    impact = swap_impact(roster, slots, args.opponent, args.opponent_sd)
    print("")
    print(f"  highest-mean lineup wins {impact['highest_mean_win_probability']:.0%}")
    print(f"  best lineup wins         {impact['best_win_probability']:.0%}"
          f"  ({impact['gain']:+.1%})")

    if args.simulate:
        result = simulate(outcome.players, args.opponent, args.opponent_sd)
        print("")
        print(f"  simulation: {result}")
    return EXIT_OK


def cmd_playoffs(ctx: Context, args) -> int:
    """Playoff odds for every team, by simulating the rest of the season."""
    from src.analytics.season_sim import Matchup, TeamSeason, simulate

    season = ctx.season
    week = args.week if args.week is not None else ctx.current_week()
    settings = ctx.settings()
    spots = int(settings.get("num_playoff_teams") or 6)
    final_week = int(settings.get("playoff_start_week") or 15) - 1

    standings = ctx.conn.fetchall(
        "SELECT team_key, team_name, wins, losses, ties, points_for "
        "FROM standings_history WHERE league_key=? AND season=? "
        "AND week=(SELECT MAX(week) FROM standings_history "
        "          WHERE league_key=? AND season=?)",
        (ctx.league_key, season, ctx.league_key, season),
    )
    if not standings:
        print("No standings stored yet.")
        print("This needs Yahoo data: run `fcc sync-settings` once access is")
        print("approved, then the Monday recap job records standings each week.")
        return EXIT_OK

    # Each team scores at the rate it has been scoring, with the spread implied
    # by the league. Better than a league-average assumption, and it is what is
    # actually knowable before any games in the remaining schedule are played.
    teams = []
    for row in standings:
        played = max(1, (row["wins"] or 0) + (row["losses"] or 0) + (row["ties"] or 0))
        mean = float(row["points_for"] or 0) / played
        teams.append(
            TeamSeason(
                team_key=str(row["team_key"]), name=row["team_name"] or row["team_key"],
                wins=int(row["wins"] or 0), losses=int(row["losses"] or 0),
                ties=int(row["ties"] or 0), points_for=float(row["points_for"] or 0),
                mean=mean if mean > 0 else 100.0, sd=max(12.0, mean * 0.22),
            )
        )

    stored = ctx.conn.fetchall(
        "SELECT week, team_key, opponent_key FROM matchups "
        "WHERE league_key=? AND season=? AND week>? ORDER BY week",
        (ctx.league_key, season, week),
    )
    seen = set()
    remaining = []
    for row in stored:
        pair = tuple(sorted((str(row["team_key"]), str(row["opponent_key"] or ""))))
        if not pair[1] or (row["week"], pair) in seen:
            continue
        seen.add((row["week"], pair))
        remaining.append(Matchup(int(row["week"]), pair[0], pair[1]))

    if not remaining:
        print(f"No remaining schedule stored beyond week {week}; nothing to simulate.")
        return EXIT_OK

    results = simulate(teams, remaining, playoff_spots=spots, trials=args.trials)
    print(f"Playoff odds after week {week} "
          f"({len(remaining)} games left, {spots} spots, {args.trials:,} simulations)")
    print("")
    mine = ctx.team_key()
    for o in results:
        marker = " <- you" if mine and o.team_key == str(mine) else ""
        print(f"  {o.describe()}{marker}")
    return EXIT_OK


def cmd_faab(ctx: Context, args) -> int:
    """What to bid, and what it will take, based on how this league bids."""
    from src.analytics import faab

    season = ctx.season
    week = args.week if args.week is not None else ctx.current_week()
    settings = ctx.settings()
    budget_total = int(settings.get("faab_budget") or 100)

    my_key = str(ctx.team_key() or "")
    if not my_key:
        # Without it every profile in the league - including yours - lands in
        # the rival list, so the model prices the player against a field that
        # contains you, and reports you among the "likely rivals".
        print("league.my_team_id is not set, so your own team cannot be excluded")
        print("from the field. Set it in config.yaml for an accurate read.")
        print("")

    # Team names and remaining budgets, when Yahoo has been synced.
    teams = {
        str(r["team_key"]): (r["team_name"] or f"Team {r['team_key']}")
        for r in ctx.conn.fetchall(
            "SELECT DISTINCT team_key, team_name FROM rosters WHERE league_key=?",
            (ctx.league_key,),
        )
    }
    # Synced balances first; --budgets then overrides individual teams, so the
    # flag stays useful for what-ifs without being the only way to supply them.
    # Left unsupplied, every hard-ceiling branch in the model is skipped and the
    # headline "who can actually afford him" constraint does nothing at all.
    budgets: dict[str, int] = {
        str(r["team_key"]): int(r["faab_balance"])
        for r in ctx.conn.fetchall(
            "SELECT team_key, faab_balance FROM team_budgets "
            "WHERE league_key=? AND season=?",
            (ctx.league_key, season),
        )
        if r["faab_balance"] is not None
    } if ctx.conn.table_exists("team_budgets") else {}

    my_budget = args.budget
    if my_budget is None:
        my_budget = budgets.get(my_key, budget_total)
    if args.budgets:
        for pair in args.budgets.split(","):
            if ":" not in pair:
                continue
            key, amount = pair.split(":", 1)
            try:
                budgets[key.strip()] = int(amount.strip())
            except ValueError:
                print(f"Ignoring unreadable budget entry {pair.strip()!r} "
                      "(expected team:amount, e.g. 3:40).")

    records = faab.parse_bids(ctx.conn, ctx.league_key)
    if records:
        faab.attach_values(ctx.conn, records, season)
    profiles = faab.learn_profiles(records, teams, budgets)

    for line in faab.league_report(profiles):
        print(line)
    print("")

    if not args.player:
        if not records:
            print("No bids observed yet. Yahoo publishes the winning bid on each")
            print("claim, so profiles sharpen after a few weeks of waivers.")
            print("")
            print("Pass a player name to get a bid recommendation:")
            print("  python fcc.py faab \"Jaylen Warren\" --budget 73")
        return EXIT_OK

    # Value the target the same way the waiver job does: what he adds over the
    # worst player you would otherwise roster.
    row = ctx.conn.fetchone(
        "SELECT p.player_key, p.full_name, p.position, "
        "       COALESCE(b.points, j.points, 0) AS points "
        "FROM players p "
        "LEFT JOIN projections_blended b "
        "       ON b.player_key=p.player_key AND b.season=? AND b.week=0 "
        "LEFT JOIN projections j "
        "       ON j.player_key=p.player_key AND j.season=? AND j.week=0 "
        "      AND j.source='sleeper' "
        "WHERE LOWER(p.full_name)=LOWER(?) LIMIT 1",
        (season, season, args.player),
    )
    if not row:
        print(f"No player called {args.player!r} in the database.")
        return EXIT_FAIL

    # Same measure as the waiver job: what the claim does to your STARTING
    # LINEUP. The two commands recommend bids on the same player and used to
    # disagree, because this one measured him against your worst bench spot -
    # which credits a backup quarterback with a full starter's value.
    value = None
    measure = "value over your worst roster spot"
    if args.replacement is None and my_key:
        from src.season import waivers as _w

        roster = _w.load_my_droppables(ctx.conn, ctx.league_key, my_key, season, week)
        slots = ctx.starting_slots()
        if roster and slots:
            target = _w.Candidate(
                player_key=row["player_key"], name=row["full_name"],
                position=row["position"], team="", ros_points=float(row["points"] or 0),
                pct_owned=0.0, trending_add=0, injury_status=None, bye_week=None,
            )
            keep = [
                d for d in roster
                if d.player_key not in _w._protected_keys(roster, slots)
            ]
            after = [d for d in roster if not keep or d.player_key != keep[0].player_key]
            after.append(target)
            value = max(
                0.0, _w._lineup_total(after, slots) - _w._lineup_total(roster, slots)
            )
            measure = "what he adds to your starting lineup"

    if value is None:
        baseline = args.replacement
        if baseline is None:
            # Only players who actually HAVE a projection can define the
            # baseline. Coalescing a missing projection to zero made the worst
            # roster spot look like a zero-point player, which it is not.
            worst = ctx.conn.fetchone(
                "SELECT MIN(b.points) AS pts FROM rosters r "
                "JOIN projections_blended b "
                "  ON b.player_key=r.player_key AND b.season=? AND b.week=0 "
                "WHERE r.league_key=? AND r.team_key=? AND r.week=? "
                "  AND b.points IS NOT NULL",
                (season, ctx.league_key, my_key, week),
            )
            baseline = float(worst["pts"]) if worst and worst["pts"] is not None else 0.0
        value = max(0.0, float(row["points"] or 0) - baseline)

    weeks_left = max(1, 15 - week)
    rivals = [pr for key, pr in profiles.items() if key != my_key]

    advice = faab.recommend(
        value=value, my_budget=my_budget, rivals=rivals, weeks_left=weeks_left
    )

    print(f"__{row['full_name']} ({row['position']})__")
    print(f"  {measure:<36}: {advice.value:.1f}")
    print(f"  {'your budget':<36}: ${my_budget} ({weeks_left} weeks left)")
    print("")
    print(f"  RECOMMENDED BID  ${advice.recommended}   "
          f"({advice.win_probability:.0%} to win)")
    print(f"  worth to you ${advice.worth_to_you}  |  "
          f"going rate ${advice.price_to_win}  |  "
          f"competitive from ${advice.min_competitive}")
    if advice.walk_away:
        print("  VERDICT: likely to cost well past his value - let him go unless "
              "he fills a genuine hole")
    if advice.contenders:
        print(f"  likely rivals: {', '.join(advice.contenders)}")
    for note in advice.notes:
        print(f"  note: {note}")

    if args.curve:
        print("")
        print("  win probability by bid:")
        for bid, probability in advice.curve:
            if bid % max(1, len(advice.curve) // 12) == 0 or bid == advice.recommended:
                marker = "  <- recommended" if bid == advice.recommended else ""
                bar = "#" * int(probability * 30)
                print(f"    ${bid:>3}  {probability:5.0%} {bar}{marker}")
    return EXIT_OK


def cmd_test_notify(ctx: Context, args) -> int:
    """Send a test notification through every configured channel.

    Exists because the real jobs only speak when they have something to say, so
    a quiet run is ambiguous: it could mean "nothing to report" or "delivery is
    broken". This makes the delivery path itself testable, from wherever it is
    run - which matters most in CI, where a misnamed secret otherwise fails
    silently until the first Tuesday of the season.
    """
    from src.storage import database_url

    notifier = ctx.notifier()
    channels = [
        name for name in ("discord", "email", "desktop")
        if ctx.cfg.get(f"notifications.{name}.enabled", False)
    ]
    if not channels:
        print("No notification channels are enabled in config.yaml.")
        return EXIT_OK

    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "this machine"
    print(f"Enabled channels: {', '.join(channels)}")

    notification = Notification(
        title="Delivery check",
        subtitle=f"Sent from {where}",
        job="test",
        season=ctx.season,
        lines=[
            f"  Sent from: {where}",
            f"  Database:  {db.describe_backend()}",
            f"  Channels:  {', '.join(channels)}",
            "",
            "  If this arrived, scheduled summaries will reach you the same way.",
        ],
    )
    # force=True: a delivery check must not be swallowed by the dedup window.
    result = notifier.send(notification, force=True)
    print(f"Result: {result}")

    if result.get("errors"):
        for channel, error in result["errors"].items():
            print(f"  {channel} FAILED: {error}")
        return EXIT_FAIL
    if not result.get("sent"):
        print("Nothing was delivered - check credentials for the enabled channels.")
        return EXIT_FAIL
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

    p_syncl = sub.add_parser(
        "sync-league", help="pull rosters, free agents and transactions from Yahoo"
    )
    p_syncl.add_argument("--force", action="store_true", help="bypass caches")
    p_syncl.add_argument("--week", type=int)

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

    for job in ("injuries", "lineup", "waivers", "byes", "recap", "trades", "reminders"):
        p_job = sub.add_parser(job, help=f"run the {job} job")
        p_job.add_argument("--week", type=int)
        p_job.add_argument("--dry-run", action="store_true")
        p_job.add_argument("--budget", type=int, help="FAAB budget remaining")
        p_job.set_defaults(job=job)

    p_daily = sub.add_parser("daily", help="sync and run everything due today")
    p_daily.add_argument("--week", type=int)
    p_daily.add_argument("--dry-run", action="store_true")

    p_mig = sub.add_parser("migrate", help="copy local SQLite into Postgres")
    p_mig.add_argument("--to", help="target postgres URL (default: DATABASE_URL)")
    p_mig.add_argument("--source", help="source sqlite file (default: config paths.db)")
    p_mig.add_argument("--dry-run", action="store_true")

    p_usage = sub.add_parser("sync-usage", help="pull opportunity, practice reports, odds")
    p_usage.add_argument("--season", type=int)
    p_usage.add_argument("--week", type=int)
    p_usage.add_argument("--force", action="store_true")

    p_reg = sub.add_parser("regression", help="buy-low / sell-high candidates")
    p_reg.add_argument("--season", type=int)
    p_reg.add_argument("--week", type=int)
    p_reg.add_argument("--window", type=int, default=6, help="trailing games to judge")
    p_reg.add_argument("--limit", type=int, default=8)

    p_acc = sub.add_parser("accuracy", help="score each projection source against reality")
    p_acc.add_argument("--season", type=int)
    p_acc.add_argument("--week", type=int)

    p_ss = sub.add_parser("startsit", help="lineup that maximises win probability")
    p_ss.add_argument("--week", type=int)
    p_ss.add_argument("--opponent", type=float, default=110.0,
                      help="opponent projected total")
    p_ss.add_argument("--opponent-sd", type=float, default=20.0)
    p_ss.add_argument("--simulate", action="store_true", help="also run Monte Carlo")

    p_po = sub.add_parser("playoffs", help="playoff odds from a season simulation")
    p_po.add_argument("--week", type=int)
    p_po.add_argument("--trials", type=int, default=5000)

    p_faab = sub.add_parser("faab", help="what to bid, and what it will take")
    p_faab.add_argument("player", nargs="?", help="player to bid on")
    p_faab.add_argument("--budget", type=int, help="your FAAB left "
                        "(defaults to the league budget)")
    p_faab.add_argument("--week", type=int)
    p_faab.add_argument("--replacement", type=float,
                        help="season points of the player he would replace")
    p_faab.add_argument("--budgets", help="rival budgets, e.g. 1:40,2:12,3:0")
    p_faab.add_argument("--curve", action="store_true",
                        help="show win probability at every bid")

    sub.add_parser("test-notify", help="send a test notification on every channel")
    sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    return parser


# Command -> handler. Module level so the parser and the dispatch table can be
# checked against each other; a hand-maintained copy of this list in the tests
# passed while `sync-league` had no handler at all.
HANDLERS = {
    "doctor": cmd_doctor,
    "sync": cmd_sync,
    "sync-league": cmd_sync_league,
    "sync-settings": cmd_sync_settings,
    "verify-settings": cmd_verify_settings,
    "rank": cmd_rank,
    "draft": cmd_draft,
    "mockdraft": cmd_mockdraft,
    "daily": cmd_daily,
    "sync-usage": cmd_sync_usage,
    "regression": cmd_regression,
    "accuracy": cmd_accuracy,
    "startsit": cmd_startsit,
    "playoffs": cmd_playoffs,
    "faab": cmd_faab,
    "test-notify": cmd_test_notify,
    "migrate": cmd_migrate,
    "dashboard": cmd_dashboard,
}


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

    handlers = HANDLERS
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
