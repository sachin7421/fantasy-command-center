"""The season dashboard: four pages built from the live league.

    My Team   record, this week's matchup in points and dollars, the roster
              with Yahoo's tags and the consensus view, the best lineup and
              what to change
    Moves     waiver claims from the live wire, stashes and handcuffs, bench
              strength against the wire, bye and injury coverage ahead
    League    standings, every team's expected payout, playoff odds, calendar
    Model     which source has been right, the weights it earned, data age

Everything Yahoo-derived is read for the page and held in memory for ten
minutes (dashboard._fetch_live_league); nothing is written. The table
builders at the top are pure so they can be tested without a browser.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from src import league_bootstrap
from src.analytics.distributions import PlayerForecast

SLOT_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "W/R/T": 4, "DEF": 5, "BN": 8, "IR": 9}


# --- pure table builders -------------------------------------------------------

def roster_rows(
    forecasts: list[PlayerForecast],
    slot_of: dict[str, str | None],
    tags: dict[str, str],
    consensus_json: dict[str, str],
    ranges: dict[str, tuple[float, float]],
    sources: dict[str, int],
) -> list[dict[str, Any]]:
    """One row per rostered player: starters by slot, bench, then IR."""
    rows: list[dict[str, Any]] = []
    order: list[tuple[int, float]] = []
    for f in forecasts:
        detail: dict[str, Any] = {}
        raw = consensus_json.get(f.player_key)
        if raw:
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = {}
        lo, hi = ranges.get(f.player_key, (None, None))
        slot = slot_of.get(f.player_key) or "BN"
        rows.append({
            "Slot": slot,
            "Player": f.name,
            "Pos": f.position,
            "Team": f.team,
            "Opp": detail.get("opponent") or "",
            "Tag": tags.get(f.player_key, "") or "",
            "Proj": round(f.mean, 1),
            "Range": f"{max(0.0, lo):.0f}-{hi:.0f}" if lo is not None and hi is not None else "",
            "Grade": detail.get("grade") or "",
            "ECR": detail.get("ecr"),
            "Sources": sources.get(f.player_key, 0),
        })
        order.append((SLOT_ORDER.get(slot, 7), -f.mean))
    return [row for _, row in sorted(zip(order, rows, strict=True), key=lambda pair: pair[0])]


def bench_strength_rows(
    bench: list[tuple[str, str, float]],
    best_free: dict[str, tuple[str, float]],
) -> list[dict[str, Any]]:
    """Each bench player against the best free agent at his position, on
    rest-of-season points. 'upgrade available' when the wire is better."""
    rows: list[dict[str, Any]] = []
    gaps: list[float] = []
    for name, position, ros in bench:
        free_name, free_ros = best_free.get(position, ("", 0.0))
        gap = round(free_ros - ros, 1)
        gaps.append(gap)
        rows.append({
            "Bench player": name,
            "Pos": position,
            "ROS pts": round(ros, 1),
            "Best free agent": free_name,
            "FA ROS pts": round(free_ros, 1),
            "Gap": gap,
            "Verdict": "upgrade available" if gap > 5 else "fine",
        })
    return [row for _, row in sorted(zip(gaps, rows, strict=True), key=lambda pair: -pair[0])]


def calendar_rows(week: int) -> list[dict[str, Any]]:
    """Where the season stands, in the terms the money is paid in."""
    pay = league_bootstrap.PAYOUTS
    regular = pay["regular_season_weeks"]
    rows = [
        {"Item": "Current week", "Value": str(week)},
        {"Item": "Regular season", "Value": f"weeks 1-{regular} ({max(0, regular - week + 1)} left incl. this one)"},
        {"Item": "Playoffs", "Value": f"weeks {league_bootstrap.PLAYOFF_WEEKS[0]}-{league_bootstrap.PLAYOFF_WEEKS[-1]}, "
                                      f"{league_bootstrap.PLAYOFF_TEAMS} teams"},
        {"Item": "Trade deadline", "Value": league_bootstrap.TRADE_DEADLINE},
        {"Item": "Weekly high score", "Value": f"${pay['weekly_high_score']} x {pay['weekly_high_score_weeks']} weeks"
                                               " (which week is unpaid: ask the commissioner)"},
        {"Item": "Most points (regular season)", "Value": f"${pay['most_points_regular_season']}"},
        {"Item": "Finish", "Value": ", ".join(f"{k}: ${v}" for k, v in league_bootstrap.FINISH_PAYOUTS.items())
                                    + f"; consolation winner ${pay['consolation_winner']}; last place -${pay['last_place_penalty']}"},
    ]
    try:
        days = (date.fromisoformat(league_bootstrap.TRADE_DEADLINE) - datetime.now(UTC).date()).days
        rows[3]["Value"] += f" ({days} days)" if days >= 0 else " (passed)"
    except ValueError:
        pass
    return rows


# --- Streamlit pages -------------------------------------------------------------

def _tile(st, label: str, value: str, note: str = "", tone: str = "") -> None:
    cls = {"good": "fcc-good", "bad": "fcc-bad", "warn": "fcc-warnc"}.get(tone, "")
    st.markdown(
        f"<div class='fcc-tile'><div class='fcc-tile-label'>{label}</div>"
        f"<div class='fcc-tile-value {cls}'>{value}</div>"
        f"<div class='fcc-tile-note'>{note}</div></div>",
        unsafe_allow_html=True,
    )


def _section(st, title: str) -> None:
    st.markdown(f"<div class='fcc-section'>{title}</div>", unsafe_allow_html=True)


def page_my_team(st, ctx, snapshot, season: int, week: int, slots: dict[str, int],
                 stakes_fn, forecasts_fn) -> None:
    """Record, this week in points and dollars, the roster, the best lineup."""
    from src.season import lineup as lineup_job

    team_key = str(ctx.team_key())
    standing = None
    try:
        setup, _ = stakes_fn.season_setup(ctx, week)
        if setup:
            standing = next((t for t in setup["teams"] if t.team_key == team_key), None)
    except Exception as exc:  # the page must render without the simulation
        st.caption(f"Standings unavailable: {exc}")
        setup = None

    verdict = None
    try:
        verdict = stakes_fn.week_stakes(ctx, snapshot, team_key, season, week, slots)
    except Exception as exc:
        st.caption(f"This week's stakes unavailable: {exc}")

    cols = st.columns(4)
    with cols[0]:
        if standing:
            rank = sorted(setup["teams"], key=lambda t: (-t.wins, -t.points_for)).index(standing) + 1
            _tile(st, "Record", f"{standing.wins}-{standing.losses}" + (f"-{standing.ties}" if standing.ties else ""),
                  f"{rank}{'st' if rank == 1 else 'nd' if rank == 2 else 'rd' if rank == 3 else 'th'} of {len(setup['teams'])}, "
                  f"{standing.points_for:.0f} pts")
        else:
            _tile(st, "Record", "-", "no standings")
    report = lineup_job.run(ctx.conn, ctx.league_key, team_key, season, week, slots,
                            snapshot=snapshot, risk_mode=verdict[0] if verdict else "auto")
    with cols[1]:
        _tile(st, "Projected", f"{report.current_points:.1f}",
              f"best legal {report.optimal_points:.1f} ({report.gain:+.1f})",
              tone="warn" if report.gain >= 1.5 else "")
    with cols[2]:
        if verdict:
            _tile(st, "This week", verdict[1][0].split(":")[1].split(",")[0].strip() if ":" in verdict[1][0] else "",
                  verdict[1][0].split("(")[0].strip())
        else:
            _tile(st, "This week", "-", "opponent unknown")
    with cols[3]:
        _tile(st, "Posture", report.risk_mode, "what the dollars call for")
    if verdict:
        st.caption(verdict[1][0])

    _section(st, "Roster")
    forecasts = forecasts_fn(ctx, season, week, snapshot.roster_keys(team_key))
    keys = [f.player_key for f in forecasts]
    slot_of = {s.player_key: s.selected_pos for s in snapshot.roster_spots_for(team_key)}
    tags = {k: snapshot.statuses.get(k, "") for k in keys}
    consensus, ranges, sources = _context_for(ctx.conn, season, week, keys)
    st.dataframe(roster_rows(forecasts, slot_of, tags, consensus, ranges, sources),
                 width="stretch", hide_index=True)

    _section(st, "Best legal lineup")
    if report.swaps:
        for swap in report.swaps:
            st.markdown(f"**{swap.slot}** start **{swap.bench_in.name}** over "
                        f"{swap.starter_out.name if swap.starter_out else '(empty)'} `{swap.gain:+.1f}`")
            for reason in swap.reasons[:2]:
                st.caption(f"+ {reason}")
    else:
        st.success("Your lineup is already the best legal one for this week.")
    for warning in report.warnings:
        st.warning(warning)
    rows = []
    for slot in report.optimal.slots:
        p = slot.player
        rows.append({"Slot": slot.slot, "Player": p.name if p else "(empty)",
                     "Pos": p.position if p else "", "Proj": round(p.points, 1) if p else None,
                     "Tag": (p.injury_status or "") if p else ""})
    st.dataframe(rows, width="stretch", hide_index=True)


def _context_for(conn, season: int, week: int, keys: list[str]):
    """Consensus detail, blend range and source count per player."""
    if not keys:
        return {}, {}, {}
    from src.yahoo_snapshot import key_clause

    clause, params = key_clause(keys)
    consensus = {
        r["player_key"]: r["stats_json"]
        for r in conn.fetchall(
            f"SELECT player_key, stats_json FROM projections WHERE source='fantasypros' "
            f"AND season=? AND week=? AND player_key IN ({clause})", (season, week, *params))
    }
    ranges, sources = {}, {}
    for r in conn.fetchall(
        f"SELECT player_key, floor, ceiling, n_sources FROM projections_blended "
        f"WHERE season=? AND week=? AND player_key IN ({clause})", (season, week, *params)):
        ranges[r["player_key"]] = (float(r["floor"] or 0), float(r["ceiling"] or 0))
        sources[r["player_key"]] = int(r["n_sources"] or 0)
    return consensus, ranges, sources


def page_moves(st, ctx, snapshot, season: int, week: int, slots: dict[str, int], waivers_fn) -> None:
    """Claims from the live wire, bench strength, and the weeks ahead."""
    from src.season import byes, waivers

    team_key = str(ctx.team_key())
    _section(st, "Waiver wire")
    waivers_fn(ctx.cfg, ctx.conn, ctx.league_key, season, week, slots, team_key, snapshot)

    _section(st, "Bench strength vs the wire (rest-of-season points)")
    try:
        free = waivers.load_free_agents(ctx.conn, season, week, snapshot.free_agents,
                                        statuses=snapshot.statuses)
        mine = waivers.load_my_droppables(ctx.conn, season, week, snapshot.roster_keys(team_key))
        starters = {s.player_key for s in snapshot.roster_spots_for(team_key)
                    if s.selected_pos not in (None, "BN", "IR")}
        bench = [(c.name, c.position, c.ros_points) for c in mine if c.player_key not in starters]
        best_free: dict[str, tuple[str, float]] = {}
        for c in free:
            if c.is_stash:
                continue
            if c.position not in best_free or c.ros_points > best_free[c.position][1]:
                best_free[c.position] = (c.name, c.ros_points)
        rows = bench_strength_rows(bench, best_free)
        st.dataframe(rows, width="stretch", hide_index=True)
    except Exception as exc:
        st.caption(f"Bench comparison unavailable: {exc}")

    _section(st, "The weeks ahead: byes and injuries")
    report = byes.run(ctx.conn, ctx.league_key, team_key, season, week, slots,
                      playoff_weeks=league_bootstrap.PLAYOFF_WEEKS, snapshot=snapshot)
    rows = []
    for w in report.weeks:
        rows.append({
            "Week": w.week,
            "Status": "OK" if w.can_fill_lineup else "GAP",
            "Available": w.available,
            "On bye": ", ".join(w.on_bye) or "",
            "Out": ", ".join(w.injured_out) or "",
            "Fix": "; ".join(w.suggestions) or "",
        })
    st.dataframe(rows, width="stretch", hide_index=True)
    if report.playoff:
        with st.expander("Playoff-week matchups for your players"):
            st.dataframe([
                {"Player": p.player, "Pos": p.position, "Team": p.team,
                 **{f"W{w}": opp for w, opp in sorted(p.opponents.items())}}
                for p in report.playoff
            ], width="stretch", hide_index=True)


def page_league(st, ctx, week: int, season_setup_fn) -> None:
    """Standings, expected payout per team, playoff odds, the calendar."""
    from src.analytics.payout import simulate_payouts
    from src.analytics.season_sim import simulate

    setup, reason = season_setup_fn(ctx, week)
    if setup is None:
        st.warning(reason)
        return
    teams = sorted(setup["teams"], key=lambda t: (-t.wins, -t.points_for))
    mine = str(ctx.team_key() or "")
    if setup["remaining"]:
        odds = simulate_payouts(setup["teams"], setup["remaining"], league_bootstrap.PAYOUTS,
                                league_bootstrap.FINISH_PAYOUTS, playoff_spots=setup["spots"],
                                trials=2000, final_week=setup["final_week"], my_team=mine or None,
                                reseed=setup["reseed"])
        po = {o.team_key: o for o in simulate(setup["teams"], setup["remaining"], setup["spots"],
                                               trials=2000, reseed=setup["reseed"])}
        pay = {o.team_key: o for o in odds}
        me = pay.get(mine)
        if me:
            cols = st.columns(4)
            with cols[0]:
                _tile(st, "Expected payout", f"${me.expected_dollars:.0f}", "rest of season, all prizes")
            with cols[1]:
                _tile(st, "Playoffs", f"{me.p_playoffs:.0%}", f"title {me.p_finish.get(1, 0):.0%}")
            with cols[2]:
                _tile(st, "Most points", f"{me.p_most_points:.0%}", f"${league_bootstrap.PAYOUTS['most_points_regular_season']}")
            with cols[3]:
                _tile(st, "Last place", f"{me.p_last:.0%}", f"-${league_bootstrap.PAYOUTS['last_place_penalty']}",
                      tone="bad" if me.p_last > 0.1 else "")
        _section(st, "Standings and expected payout")
        st.dataframe([
            {"#": i, "Team": t.name + (" (you)" if t.team_key == mine else ""),
             "W-L": f"{t.wins}-{t.losses}", "PF": round(t.points_for, 1),
             "Pts/wk": round(t.mean, 1),
             "Playoffs": f"{po[t.team_key].playoff_odds:.0%}" if t.team_key in po else "",
             "Title": f"{pay[t.team_key].p_finish.get(1, 0):.0%}" if t.team_key in pay else "",
             "Most pts": f"{pay[t.team_key].p_most_points:.0%}" if t.team_key in pay else "",
             "Exp. $": round(pay[t.team_key].expected_dollars) if t.team_key in pay else None}
            for i, t in enumerate(teams, 1)
        ], width="stretch", hide_index=True)
    else:
        st.info("The regular season is over; seeding is decided.")
    _section(st, "Calendar and money")
    st.dataframe(calendar_rows(week), width="stretch", hide_index=True)


def page_model(st, conn, season: int, week: int) -> None:
    """What the model has been right about, and how fresh its data is."""
    from src.analytics import accuracy

    _section(st, "Projection accuracy by source (completed weeks)")
    results = accuracy.score_sources(conn, season, through_week=week - 1) if week > 1 else []
    if results:
        st.dataframe([
            {"Pos": r.position, "Source": r.source, "n": r.n, "MAE": r.mae, "RMSE": r.rmse,
             "Bias": r.bias}
            for r in results
        ], width="stretch", hide_index=True)
        earned = accuracy.earned_weights_from(results)
        if earned:
            _section(st, "Weights earned for this week's blend")
            st.dataframe([
                {"Pos": pos, **{s: round(w, 2) for s, w in sorted(ws.items())}}
                for pos, ws in sorted(earned.items())
            ], width="stretch", hide_index=True)
    else:
        st.caption("Accuracy needs completed weeks with both projections and actuals.")

    _section(st, "Data freshness")
    rows = conn.fetchall(
        "SELECT source, week, COUNT(*) AS n, MAX(fetched_at) AS last FROM projections "
        "WHERE season=? AND week IN (0, ?) GROUP BY source, week ORDER BY week, source",
        (season, week),
    )
    st.dataframe([{"Source": r["source"], "Week": "season" if r["week"] == 0 else str(r["week"]),
                   "Rows": r["n"], "Last fetched": str(r["last"])[:16].replace("T", " ")}
                  for r in rows], width="stretch", hide_index=True)
    runs = conn.fetchall(
        "SELECT job, status, finished_at FROM job_runs WHERE job IN ('verify-scoring','sync','sync-usage') "
        "ORDER BY finished_at DESC LIMIT 6"
    )
    if runs:
        st.caption("Recent runs: " + "; ".join(
            f"{r['job']} {r['status']} {str(r['finished_at'])[:16].replace('T', ' ')}" for r in runs))
