"""Streamlit dashboard: live draft board + season view (spec 5.3).

Run with:  python fcc.py dashboard      (or: streamlit run dashboard.py)

Built for use under time pressure - roughly ninety seconds a pick. The layout
puts the single most important thing (who to take) top-left, keeps position and
tier readable without parsing text, and reserves one colour for actions so the
Draft control is never ambiguous. See src/ui.py for the reasoning behind the
palette.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import auth, charts, db, league_bootstrap, ui, vorp
from src.config import Config
from src.draft.live import DraftTracker, resolve_player
from src.draft.recommender import DraftRecommender, RosterState
from src.draft.survival import DraftPosition

st.set_page_config(
    page_title="Fantasy Command Center",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_context():
    cfg = Config.load("config.yaml")
    # Streamlit reruns the script on a new thread each time while reusing this
    # cached connection, so it must not be pinned to its creating thread.
    # init_db picks SQLite or Postgres from DATABASE_URL and applies the
    # matching schema, so the same code serves local and hosted runs.
    conn = db.init_db(cfg.db_path, same_thread=False)
    league_key = f"nfl.l.{cfg.get('league.league_id', league_bootstrap.LEAGUE_ID)}"
    league_bootstrap.install(conn, league_key)
    return cfg, conn, league_key


@st.cache_data(ttl=60)
def load_board(_conn, season: int, slots: dict, teams: int, gap: float,
               prior_strength: float = 0.0):
    return vorp.build_board(
        _conn, season, slots, teams, tier_gap_pct=gap,
        prior_strength=prior_strength,
    )


def board_for(cfg, conn, season, slots, teams):
    """The board, built from config every time.

    Two of the three call sites passed a hardcoded tier gap, so the season tabs
    and the draft board disagreed about where tiers break whenever the setting
    was changed.
    """
    return load_board(
        conn, season, slots, teams,
        float(cfg.get("draft.tier_gap_pct", 0.08)),
        float(cfg.get("draft.prior_regression_strength", 0.0)),
    )


def settings_of(conn, league_key):
    row = conn.fetchone(
        "SELECT settings_json FROM league_settings WHERE league_key=?", (league_key,)
    )
    return json.loads(row["settings_json"]) if row else league_bootstrap.build_settings()


def starting_slots_of(settings) -> dict[str, int]:
    slots: dict[str, int] = {}
    for entry in settings.get("roster_positions") or []:
        rp = entry.get("roster_position", entry)
        pos = str(rp.get("position") or "")
        if pos and pos.upper() not in ("BN", "IR", "IR+", "NA"):
            slots[pos] = slots.get(pos, 0) + int(rp.get("count") or 0)
    return slots


def total_rounds(settings) -> int:
    total = 0
    for entry in settings.get("roster_positions") or []:
        rp = entry.get("roster_position", entry)
        if str(rp.get("position") or "").upper() in ("IR", "IR+", "NA"):
            continue
        total += int(rp.get("count") or 0)
    return total or 15


# --- draft view --------------------------------------------------------------

def draft_view(cfg, conn, league_key):
    settings = settings_of(conn, league_key)
    slots = starting_slots_of(settings)
    teams = int(settings.get("num_teams") or 12)
    rounds = total_rounds(settings)
    season = int(cfg.get("league.season") or settings.get("season") or 2026)

    with st.sidebar:
        st.markdown("<div class='fcc-section'>Draft setup</div>", unsafe_allow_html=True)
        my_slot = st.number_input(
            "Your draft slot", 1, teams, int(cfg.get("draft.draft_slot") or 1)
        )
        auto_sync = st.toggle("Poll Yahoo for picks", value=False)
        phone_layout = st.toggle(
            "Phone layout", value=False,
            help="Stack the panes into tabs for a narrow screen.",
        )
        st.caption(
            "Manual mode is the default and always works. Yahoo polling only "
            "reads picks — nothing here ever drafts for you."
        )
        st.divider()
        st.caption(f"{league_key} · {teams} teams · {rounds} rounds")
        st.caption(f"Data: {db.describe_backend()}")

    board = board_for(cfg, conn, season, slots, teams)
    if not board.players:
        st.warning("No projections stored. Run `python fcc.py sync` first.")
        return

    tracker = DraftTracker(conn, league_key, teams, rounds, None)
    if auto_sync:
        try:
            from src.yahoo_client import YahooClient

            tracker.yahoo = YahooClient(cfg, conn)
            new = tracker.sync_from_yahoo()
            if tracker.last_sync_ok:
                st.sidebar.success(f"Yahoo synced (+{new} picks)")
            else:
                st.sidebar.error(f"Yahoo sync failed: {tracker.last_error}")
        except Exception as exc:
            st.sidebar.error(f"Yahoo unavailable: {exc}")
            st.sidebar.info("Manual mode still works normally.")

    position = DraftPosition(num_teams=teams, draft_slot=int(my_slot), rounds=rounds)
    recommender = DraftRecommender(
        board,
        position,
        need_weight=float(cfg.get("draft.need_weight", 0.35)),
        defer_positions=cfg.get("draft.defer_positions", ["K", "DEF"]),
        defer_until_round=int(cfg.get("draft.defer_until_round", 12)),
        bye_stack_threshold=int(cfg.get("draft.bye_stack_warn_threshold", 3)),
        te_flex_credit=int(cfg.get("draft.te_flex_credit", 0)),
    )

    drafted = tracker.state.drafted_keys
    current_pick = tracker.state.next_pick
    my_keys = set(tracker.state.roster_of(str(my_slot)))
    my_players = [p for p in board.players if p.player_key in my_keys]
    roster = RosterState(starting_slots=slots, players=my_players)

    next_pick = position.next_pick_after(current_pick)
    on_the_clock = tracker.state.team_key_for_pick(current_pick)
    is_mine = on_the_clock == int(my_slot)

    # -- header ---------------------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pick", current_pick, f"Round {position.current_round(current_pick)}")
    c2.metric("On the clock", f"Slot {on_the_clock}", "YOU" if is_mine else None)
    c3.metric(
        "Your next pick", next_pick or "—",
        f"{next_pick - current_pick} away" if next_pick else None,
    )
    c4.metric("Off the board", len(drafted), f"of {teams * rounds}")

    if is_mine:
        st.markdown(
            "<div class='fcc-clock'>You are on the clock.</div>", unsafe_allow_html=True
        )

    # -- mark drafted ---------------------------------------------------------
    # This is the whole manual workflow, so it says so rather than relying on
    # placeholder text inside a collapsed label: every recommendation depends on
    # knowing who is off the board, and nothing else here records that.
    with st.container(border=True):
        st.markdown(
            f"<div class='fcc-section'>Record pick {current_pick} &mdash; "
            f"slot {on_the_clock}{' (you)' if is_mine else ''}</div>",
            unsafe_allow_html=True,
        )
        search_col, undo_col = st.columns([5, 1])
        with search_col:
            query = st.text_input(
                "Mark a player drafted",
                placeholder="Any player taken by anyone - type a name, then tap the match",
                label_visibility="collapsed",
            )
        with undo_col:
            if st.button("Undo", width="stretch"):
                tracker.undo_last()
                st.cache_data.clear()
                st.rerun()

        st.caption(
            "Every pick goes here, yours and everyone else's, in order. The "
            "snake order decides which team each one belongs to, so the "
            "recommendations only stay right if no pick is skipped."
        )

        if query:
            matches = [p for p in resolve_player(board, query) if p.player_key not in drafted]
            if not matches:
                st.caption("No available player matches that.")
            cols = st.columns(min(4, max(1, len(matches[:8]))))
            for i, player in enumerate(matches[:8]):
                with cols[i % len(cols)]:
                    if st.button(
                        f"{player.name} · {player.position}{player.position_rank}",
                        key=f"mark_{player.player_key}", width="stretch",
                    ):
                        tracker.record_pick(player.player_key)
                        st.cache_data.clear()
                        st.rerun()

    if phone_layout:
        tab_rec, tab_board, tab_roster, tab_shape = st.tabs(
            ["Recommended", "Board", "Roster", "Shape"]
        )
        with tab_rec:
            _pane_recommendations(recommender, drafted, roster, current_pick,
                                  tracker, my_slot, is_mine, on_the_clock)
        with tab_board:
            _pane_board(board, drafted, True, tracker, my_slot, is_mine,
                        on_the_clock, current_pick)
        with tab_roster:
            _pane_roster(roster, my_players, slots, tracker, board)
        with tab_shape:
            _pane_shape(board, drafted)
    else:
        left, middle, right = st.columns([3, 4, 2])
        with left:
            _pane_recommendations(recommender, drafted, roster, current_pick,
                                  tracker, my_slot, is_mine, on_the_clock)
        with middle:
            _pane_board(board, drafted, False, tracker, my_slot, is_mine,
                        on_the_clock, current_pick)
            _pane_shape(board, drafted)
        with right:
            _pane_roster(roster, my_players, slots, tracker, board)


# --- panes -------------------------------------------------------------------

def _pane_recommendations(recommender, drafted, roster, current_pick, tracker,
                          my_slot, is_mine, on_the_clock):
    st.markdown("<div class='fcc-section'>Recommended</div>", unsafe_allow_html=True)
    recs = recommender.recommend(drafted, roster, current_pick, top_n=10)
    if not recs:
        st.info("No recommendations — the board may be exhausted.")

    next_pick = recommender.position.next_pick_after(current_pick)

    for i, rec in enumerate(recs, 1):
        p = rec.player
        hue = ui.position_hue(p.position)

        survival_block = ""
        if rec.survival is not None and next_pick:
            survival_block = (
                f"<div style='margin-top:9px;'>"
                f"<div style='color:{ui.INK_MUTED};font-size:0.64rem;"
                f"letter-spacing:0.06em;text-transform:uppercase;margin-bottom:4px;'>"
                f"Survives to pick {next_pick}</div>"
                f"{ui.survival_bar(rec.survival)}</div>"
            )

        st.markdown(
            f"<div class='fcc-card' style='border-left-color:{hue};'>"
            f"<span class='fcc-rank'>{i}</span>"
            f"<span class='fcc-name'>{p.name}</span>&nbsp;&nbsp;"
            f"{ui.position_badge(p.position, p.position_rank)}&nbsp;&nbsp;"
            f"{ui.tier_pill(p.tier)}"
            f"<div style='margin-top:9px;'>"
            f"{ui.stat('VORP', f'{p.vorp:.1f}')}"
            f"{ui.stat('Proj', f'{p.points:.0f}')}"
            f"{ui.stat('ADP', f'{p.adp:.0f}' if p.adp else '—')}"
            f"{ui.stat('Bye', str(p.bye_week or '—'))}"
            f"{ui.stat('Team', p.team or '—')}"
            f"</div>"
            f"{survival_block}"
            + "".join(f"<div class='fcc-reason'>+ {r}</div>" for r in rec.reasons[:2])
            + "".join(f"<div class='fcc-warn'>! {w}</div>" for w in rec.warnings[:2])
            + "</div>",
            unsafe_allow_html=True,
        )
        # Two named actions rather than one button whose meaning changes with
        # whose turn it is. Both consume the current pick - the snake order
        # decides which team owns it - so the two differ only in whether the
        # player lands on my roster. A single button labelled by the team on
        # the clock read as if it were about that player, not about the pick.
        draft_col, taken_col = st.columns(2)
        with draft_col:
            if st.button(
                "Draft to me", key=f"take_{p.player_key}", width="stretch",
                type="primary" if i == 1 and is_mine else "secondary",
                disabled=not is_mine,
                help=None if is_mine else (
                    f"Pick {current_pick} belongs to slot {on_the_clock}. Record "
                    "the picks before yours first, then this becomes available."
                ),
            ):
                tracker.record_pick(p.player_key, team_key=str(my_slot))
                st.cache_data.clear()
                st.rerun()
        with taken_col:
            if st.button(
                "Taken", key=f"gone_{p.player_key}", width="stretch",
                help=f"Someone else took him with pick {current_pick} "
                     f"(slot {on_the_clock}).",
            ):
                tracker.record_pick(p.player_key)
                st.cache_data.clear()
                st.rerun()


def _pane_board(board, drafted, phone_layout, tracker, my_slot, is_mine,
                on_the_clock, current_pick):
    st.markdown("<div class='fcc-section'>Best available</div>", unsafe_allow_html=True)
    positions = ["ALL"] + sorted({p.position for p in board.players})
    chosen = st.radio("Position", positions, horizontal=True, label_visibility="collapsed")
    pool = [p for p in board.available(drafted)
            if chosen == "ALL" or p.position == chosen][:60]
    if not pool:
        st.caption("Nothing available at that position.")
        return

    rows = []
    for p in pool:
        row = {
            "Tier": p.tier,
            "Player": p.name,
            "Pos": f"{p.position}{p.position_rank}",
            "Proj": round(p.points, 1),
            "VORP": round(p.vorp, 1),
        }
        if not phone_layout:
            row.update({
                "Tm": p.team,
                "ADP": round(p.adp, 1) if p.adp else None,
                "Bye": p.bye_week,
                "Status": p.injury_status or "",
            })
        rows.append(row)

    # Selectable, so any of the sixty is one tap from being recorded. The
    # recommendation list only carries ten, and a draft regularly goes off
    # board - somebody reaches, and without a way to mark him every number
    # after that is computed from a board that is wrong.
    selection = st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
        height=380 if phone_layout else 440,
        on_select="rerun",
        selection_mode="single-row",
        key=f"board_{'phone' if phone_layout else 'wide'}",
        column_config={
            "Tier": st.column_config.NumberColumn(width="small"),
            "VORP": st.column_config.ProgressColumn(
                format="%.1f",
                min_value=float(min(r["VORP"] for r in rows)),
                max_value=float(max(r["VORP"] for r in rows)),
            ),
        },
    )

    chosen_rows = selection.selection.rows if selection and selection.selection else []
    if not chosen_rows:
        st.caption("Select a row to draft him or mark him taken.")
        return

    chosen = pool[chosen_rows[0]]
    st.markdown(
        f"**{chosen.name}** · {chosen.position}{chosen.position_rank} · "
        f"{chosen.points:.0f} proj"
    )
    draft_col, taken_col = st.columns(2)
    with draft_col:
        if st.button(
            "Draft to me", key=f"board_take_{chosen.player_key}", width="stretch",
            type="primary" if is_mine else "secondary", disabled=not is_mine,
            help=None if is_mine else
                 f"Pick {current_pick} belongs to slot {on_the_clock}.",
        ):
            tracker.record_pick(chosen.player_key, team_key=str(my_slot))
            st.cache_data.clear()
            st.rerun()
    with taken_col:
        if st.button("Taken", key=f"board_gone_{chosen.player_key}",
                     width="stretch"):
            tracker.record_pick(chosen.player_key)
            st.cache_data.clear()
            st.rerun()


def _pane_shape(board, drafted):
    """The scarcity picture: where value falls off at each position."""
    st.markdown(
        "<div class='fcc-section'>Value curve — where each position falls off</div>",
        unsafe_allow_html=True,
    )
    chart = charts.value_curve(board.available(drafted))
    if chart is None:
        st.caption("Not enough data to plot.")
        return
    st.altair_chart(chart, width='stretch')
    st.caption(
        "Steeper means waiting costs more. The dashed rule is replacement level — "
        "where a line crosses it, that position stops being worth a starting slot."
    )

    chips = "".join(
        f"<span class='fcc-slot' style='background:"
        f"{ui.tint(ui.position_hue(pos), 0.18)};color:{ui.INK};'>"
        f"{pos}&nbsp;&nbsp;{value:.1f} pts/pick</span>"
        for pos, value in sorted(board.scarcity.items(), key=lambda kv: -kv[1])
    )
    st.markdown(chips, unsafe_allow_html=True)


def _pane_roster(roster, my_players, slots, tracker, board):
    st.markdown("<div class='fcc-section'>My roster</div>", unsafe_allow_html=True)

    unfilled = roster.unfilled()
    counts = roster.counts()
    chips = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        if pos not in counts and pos not in slots:
            continue
        open_count = unfilled.get(pos, 0)
        klass = "fcc-slot-open" if open_count > 0 else "fcc-slot-filled"
        label = f"{pos} {counts.get(pos, 0)}"
        if open_count > 0:
            label += f" <span style='opacity:0.7'>+{open_count:.1f}</span>"
        chips.append(f"<span class='fcc-slot {klass}'>{label}</span>")
    st.markdown("".join(chips), unsafe_allow_html=True)

    if my_players:
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    for p in my_players:
        hue = ui.position_hue(p.position)
        st.markdown(
            f"<div style='border-left:3px solid {hue};padding:4px 0 4px 9px;"
            f"margin-bottom:6px;'>"
            f"<div style='font-weight:650;font-size:0.9rem;'>{p.name}</div>"
            f"<div style='color:{ui.INK_MUTED};font-size:0.75rem;"
            f"font-variant-numeric:tabular-nums;'>"
            f"{p.position}{p.position_rank} · {p.points:.0f} pts · bye {p.bye_week or '—'}"
            f"</div></div>",
            unsafe_allow_html=True,
        )

    byes = roster.bye_weeks()
    stacked = {w: len(v) for w, v in byes.items() if len(v) >= 2}
    if stacked:
        st.warning(
            "Bye stacking — "
            + ", ".join(f"week {w}: {n}" for w, n in sorted(stacked.items()))
        )

    picks = sorted(tracker.state.picks.values(), key=lambda x: -x.pick)[:12]
    if picks:
        st.divider()
        st.markdown("<div class='fcc-section'>Recent picks</div>", unsafe_allow_html=True)
        for pick in picks:
            player = board.get(pick.player_key) if pick.player_key else None
            name = player.name if player else (pick.player_key or "?")
            hue = ui.position_hue(player.position) if player else ui.POSITION_FALLBACK_HUE
            st.markdown(
                f"<div style='font-size:0.78rem;color:{ui.INK_MUTED};"
                f"font-variant-numeric:tabular-nums;margin-bottom:3px;'>"
                f"<span style='color:{ui.INK_FAINT};'>{pick.pick}.</span> "
                f"<span style='color:{hue};'>&#9632;</span> {name} "
                f"<span style='color:{ui.INK_FAINT};'>slot {pick.team_key}</span></div>",
                unsafe_allow_html=True,
            )


# --- season view -------------------------------------------------------------

def _tab_edge(conn, season: int):
    """The models: who is due to regress, and which source to believe."""
    from src.analytics import accuracy, regression

    st.markdown(
        "<div class='fcc-section'>Buy low / sell high — expected points regression</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Fantasy points mix opportunity with efficiency. Opportunity persists "
        "week to week; efficiency, and touchdowns above all, mostly does not. "
        "A player scoring far from what his usage implies is likelier to move "
        "toward it than to keep going."
    )

    try:
        signals = regression.scan(conn, season)
    except Exception as exc:
        st.info(f"Usage data not loaded yet ({exc}). Run `python fcc.py sync-usage`.")
        signals = []

    if not signals:
        st.info(
            "No actionable signals. This needs several weeks of played games — "
            "run `python fcc.py sync-usage` once the season is under way."
        )
    else:
        buy = [s for s in signals if s.verdict == "buy"]
        sell = [s for s in signals if s.verdict == "sell"]
        left, right = st.columns(2)

        for column, group, label, tone in (
            (left, buy, "Buy low", ui.POSITIVE),
            (right, sell, "Sell high", ui.WARNING),
        ):
            with column:
                st.markdown(
                    f"<div style='color:{tone};font-weight:700;font-size:0.8rem;"
                    f"letter-spacing:0.06em;text-transform:uppercase;'>{label}</div>",
                    unsafe_allow_html=True,
                )
                if not group:
                    st.caption("Nothing flagged.")
                for s in group[:8]:
                    hue = ui.position_hue(s.position)
                    st.markdown(
                        f"<div class='fcc-card' style='border-left-color:{hue};'>"
                        f"<span class='fcc-name'>{s.name}</span>&nbsp;&nbsp;"
                        f"{ui.position_badge(s.position)}"
                        f"<div style='margin-top:7px;'>"
                        f"{ui.stat('Actual', f'{s.points_actual:.1f}')}"
                        f"{ui.stat('Expected', f'{s.points_expected:.1f}')}"
                        f"{ui.stat('Gap/gm', f'{s.residual:+.1f}', tone)}"
                        f"{ui.stat('Games', str(s.games))}"
                        f"{ui.stat('Conf', f'{s.confidence:.0%}')}"
                        f"</div>"
                        + "".join(
                            f"<div class='fcc-reason'>{r}</div>" for r in s.reasons[:2]
                        )
                        + "</div>",
                        unsafe_allow_html=True,
                    )

    st.divider()
    st.markdown(
        "<div class='fcc-section'>Which projection source is actually right</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Every stored projection graded against what happened, in this league's "
        "scoring. Weights are earned rather than assumed — and shrunk toward "
        "equal while the evidence is still thin."
    )
    try:
        results = accuracy.score_sources(conn, season)
    except Exception:
        results = []

    if not results:
        st.info(
            "Not enough paired projections and results yet. This becomes "
            "meaningful a few weeks into the season."
        )
    else:
        st.dataframe(
            [
                {
                    "Source": r.source, "Pos": r.position, "n": r.n,
                    "MAE": r.mae, "RMSE": r.rmse, "Bias": r.bias,
                }
                for r in results
            ],
            width="stretch", hide_index=True, height=320,
        )
        weights = accuracy.derive_weights(results)
        if weights:
            chips = "".join(
                f"<span class='fcc-slot fcc-slot-filled'>{s}&nbsp;&nbsp;{w:.2f}</span>"
                for s, w in sorted(weights.items(), key=lambda kv: -kv[1])
            )
            st.markdown("Earned blend weights: " + chips, unsafe_allow_html=True)


def season_view(cfg, conn, league_key):
    settings = settings_of(conn, league_key)
    slots = starting_slots_of(settings)
    season = int(cfg.get("league.season") or 2026)
    teams = int(settings.get("num_teams") or 12)

    st.sidebar.divider()
    st.sidebar.caption(f"Data: {db.describe_backend()}")

    tabs = st.tabs(
        ["Edge", "Activity", "Injuries", "Waiver heat", "Board", "Positional shape"]
    )

    with tabs[0]:
        _tab_edge(conn, season)


    with tabs[1]:
        st.markdown("<div class='fcc-section'>Recent job output</div>",
                    unsafe_allow_html=True)
        rows = conn.fetchall(
            "SELECT job, week, payload_json, created_at, notified_at "
            "FROM recommendations ORDER BY created_at DESC LIMIT 40"
        )
        if not rows:
            st.info(
                "No jobs have run yet. Try `python fcc.py injuries --dry-run` locally, "
                "or trigger the *Scheduled jobs* workflow from GitHub Actions."
            )
        for row in rows:
            payload = json.loads(row["payload_json"])
            sent = "notified" if row["notified_at"] else "recorded only"
            with st.expander(
                f"{payload.get('title', row['job'])}  ·  {row['created_at']}  ·  {sent}"
            ):
                st.text("\n".join(payload.get("lines", [])))

    with tabs[2]:
        rows = conn.fetchall(
            """
            SELECT p.full_name AS player, p.position, p.team, i.status,
                   i.body_part, i.observed_at
            FROM injuries i JOIN players p USING(player_key)
            JOIN (SELECT player_key, MAX(observed_at) latest FROM injuries
                  GROUP BY player_key) m
              ON m.player_key=i.player_key AND m.latest=i.observed_at
            ORDER BY CASE i.status WHEN 'IR' THEN 1 WHEN 'Out' THEN 2
                     WHEN 'Doubtful' THEN 3 WHEN 'Questionable' THEN 4 ELSE 5 END
            LIMIT 120
            """
        )
        st.dataframe([dict(r) for r in rows], width="stretch", hide_index=True, height=520)

    with tabs[3]:
        st.caption(
            "Trending adds across all Sleeper leagues — a leading indicator of who "
            "your league-mates are about to claim."
        )
        rows = conn.fetchall(
            """
            SELECT p.full_name AS player, p.position, p.team, t.count AS adds
            FROM trending t JOIN players p USING(player_key)
            JOIN (SELECT player_key, MAX(fetched_at) latest FROM trending
                  WHERE kind='add' GROUP BY player_key) m
              ON m.player_key=t.player_key AND m.latest=t.fetched_at
            WHERE t.kind='add' ORDER BY t.count DESC LIMIT 40
            """
        )
        st.dataframe([dict(r) for r in rows], width="stretch", hide_index=True, height=520)

    with tabs[4]:
        board = board_for(cfg, conn, season, slots, teams)
        st.dataframe(
            [
                {
                    "#": p.overall_rank, "Player": p.name,
                    "Pos": f"{p.position}{p.position_rank}", "Tm": p.team,
                    "Proj": round(p.points, 1), "VORP": round(p.vorp, 1),
                    "ADP": round(p.adp, 1) if p.adp else None, "Bye": p.bye_week,
                }
                for p in board.players[:200]
            ],
            width="stretch", hide_index=True, height=560,
        )

    with tabs[5]:
        board = board_for(cfg, conn, season, slots, teams)
        chart = charts.value_curve(board.players, height=380)
        if chart is not None:
            st.altair_chart(chart, width='stretch')
        available = sorted({p.position for p in board.players})
        # Default to RB: it is the scarcest position in this league, so its tier
        # structure is the one worth looking at first.
        default = available.index("RB") if "RB" in available else 0
        pos = st.selectbox("Tier breakdown", available, index=default)
        cliff = charts.tier_cliff(board.players, pos)
        if cliff is not None:
            st.altair_chart(cliff, width='stretch')

        # How one player's projection has moved, per source. Written months ago
        # and never rendered anywhere, so the projection_history table was being
        # written on every sync and read by nothing.
        st.markdown("<div class='fcc-section'>Projection drift</div>",
                    unsafe_allow_html=True)
        tracked = conn.fetchall(
            "SELECT DISTINCT h.player_key, p.full_name "
            "FROM projection_history h JOIN players p USING(player_key) "
            "WHERE h.season=? AND h.week=0 "
            "  AND p.player_key IN (SELECT player_key FROM projections_blended "
            "                       WHERE season=? AND week=0 "
            "                       ORDER BY points DESC LIMIT 150) "
            "ORDER BY p.full_name",
            (season, season),
        )
        if not tracked:
            st.caption(
                "Nothing to plot yet: this needs the same player projected on "
                "at least two different days, which builds up over a season."
            )
        else:
            names = {r["full_name"]: r["player_key"] for r in tracked}
            who = st.selectbox("Player", sorted(names), key="drift_player")
            rows = conn.fetchall(
                "SELECT source, points, observed_at FROM projection_history "
                "WHERE player_key=? AND season=? AND week=0 ORDER BY observed_at",
                (names[who], season),
            )
            drift = charts.projection_drift([dict(r) for r in rows])
            if drift is None:
                st.caption(
                    f"{who} has been projected on only one day so far - a drift "
                    "line needs at least two."
                )
            else:
                st.altair_chart(drift, width='stretch')


def main():
    # Gate first: nothing touches the database or renders league data until the
    # password is accepted. Open automatically when no password is configured.
    auth.require_password()
    ui.inject_css()

    cfg, conn, league_key = get_context()

    st.sidebar.markdown(
        "<div class='fcc-brand'>Command Center</div>"
        "<div class='fcc-brand-sub'>Butt Fumblers</div>",
        unsafe_allow_html=True,
    )
    mode = st.sidebar.radio("Mode", ["Draft", "Season"], label_visibility="collapsed")
    if mode == "Draft":
        draft_view(cfg, conn, league_key)
    else:
        season_view(cfg, conn, league_key)


main()
