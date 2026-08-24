"""Streamlit dashboard: live draft board + season view (spec 5.3).

Run with:  python fcc.py dashboard      (or: streamlit run dashboard.py)

The draft view is built for use under time pressure: three panes, big tap
targets, a search box, and an undo. Everything works whether picks arrive from
Yahoo polling or are tapped in by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import auth, db, league_bootstrap, vorp
from src.config import Config
from src.draft.live import DraftTracker, resolve_player
from src.draft.recommender import DraftRecommender, RosterState
from src.draft.survival import DraftPosition

st.set_page_config(page_title="Fantasy Command Center", layout="wide")

TIER_COLORS = [
    "#1b5e20", "#2e7d32", "#558b2f", "#827717", "#ef6c00",
    "#e65100", "#bf360c", "#4e342e", "#37474f", "#212121",
]


def tier_color(tier: int) -> str:
    return TIER_COLORS[min(max(tier, 1) - 1, len(TIER_COLORS) - 1)]


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
def load_board(_conn, season: int, slots: dict, teams: int, gap: float):
    return vorp.build_board(_conn, season, slots, teams, tier_gap_pct=gap)


def settings_of(conn, league_key):
    import json

    row = conn.execute(
        "SELECT settings_json FROM league_settings WHERE league_key=?", (league_key,)
    ).fetchone()
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
        st.header("Draft setup")
        my_slot = st.number_input(
            "Your draft slot", 1, teams, int(cfg.get("draft.draft_slot") or 1)
        )
        auto_sync = st.toggle("Poll Yahoo for picks", value=False)
        phone_layout = st.toggle(
            "Phone layout", value=False,
            help="Stack the three panes into tabs for a narrow screen.",
        )
        st.caption(
            "Leave off to run fully manually. Manual mode is the fallback if "
            "Yahoo polling fails mid-draft."
        )
        st.divider()
        st.caption(f"League {league_key}  |  {teams} teams  |  {rounds} rounds")
        st.caption(f"Data: {db.describe_backend()}")
        st.caption(f"Slots: {slots}")

    board = load_board(conn, season, slots, teams, float(cfg.get("draft.tier_gap_pct", 0.08)))
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

    # -- header ---------------------------------------------------------------
    next_pick = position.next_pick_after(current_pick)
    on_the_clock = position.num_teams and tracker.state.team_key_for_pick(current_pick)
    is_mine = on_the_clock == int(my_slot)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pick", f"{current_pick}", f"Round {position.current_round(current_pick)}")
    c2.metric("On the clock", f"Slot {on_the_clock}", "YOU" if is_mine else "")
    c3.metric("Your next pick", next_pick or "-",
              f"{next_pick - current_pick} away" if next_pick else "")
    c4.metric("Drafted", f"{len(drafted)}", f"of {teams * rounds}")

    if is_mine:
        st.success("You are on the clock.")

    # -- mark drafted ---------------------------------------------------------
    with st.container(border=True):
        search_col, undo_col = st.columns([4, 1])
        with search_col:
            query = st.text_input("Mark a player drafted", placeholder="type a name...")
        with undo_col:
            st.write("")
            if st.button("Undo last", width="stretch"):
                removed = tracker.undo_last()
                st.cache_data.clear()
                st.rerun()

        if query:
            matches = [p for p in resolve_player(board, query) if p.player_key not in drafted]
            cols = st.columns(min(4, max(1, len(matches[:8]))))
            for i, player in enumerate(matches[:8]):
                with cols[i % len(cols)]:
                    label = f"{player.name} ({player.position}{player.position_rank})"
                    if st.button(label, key=f"mark_{player.player_key}",
                                 width="stretch"):
                        tracker.record_pick(player.player_key)
                        st.cache_data.clear()
                        st.rerun()

    # Three panes side by side on a desktop. On a phone a 3-column layout is
    # unusable, so the same panes become tabs.
    if phone_layout:
        tab_rec, tab_board, tab_roster = st.tabs(["Recommended", "Board", "My roster"])
        with tab_rec:
            _pane_recommendations(recommender, drafted, roster, current_pick,
                                  tracker, my_slot, phone_layout)
        with tab_board:
            _pane_board(board, drafted, phone_layout)
        with tab_roster:
            _pane_roster(roster, my_players, slots, tracker, board)
    else:
        left, middle, right = st.columns([3, 4, 2])
        with left:
            _pane_recommendations(recommender, drafted, roster, current_pick,
                                  tracker, my_slot, phone_layout)
        with middle:
            _pane_board(board, drafted, phone_layout)
        with right:
            _pane_roster(roster, my_players, slots, tracker, board)


# --- panes -------------------------------------------------------------------

def _pane_recommendations(recommender, drafted, roster, current_pick, tracker,
                          my_slot, phone_layout):
    st.subheader("Recommended")
    recs = recommender.recommend(drafted, roster, current_pick, top_n=10)
    if not recs:
        st.info("No recommendations - the board may be exhausted.")
    for i, rec in enumerate(recs, 1):
        p = rec.player
        with st.container(border=True):
            bits = [f"VORP {p.vorp:.1f}", f"proj {p.points:.0f}"]
            if p.adp:
                bits.append(f"ADP {p.adp:.0f}")
            if p.bye_week:
                bits.append(f"bye {p.bye_week}")
            if rec.survival is not None:
                bits.append(f"survives {rec.survival:.0%}")

            def _body():
                st.markdown(
                    f"**{i}. {p.name}** "
                    f"<span style='color:{tier_color(p.tier)}'>&#9632;</span> "
                    f"{p.position}{p.position_rank} &middot; T{p.tier} &middot; {p.team}",
                    unsafe_allow_html=True,
                )
                st.caption(" &middot; ".join(bits))
                for reason in rec.reasons[:2]:
                    st.caption(f":green[+ {reason}]")
                for warning in rec.warnings[:2]:
                    st.caption(f":orange[! {warning}]")

            def _button():
                if st.button("Draft", key=f"take_{p.player_key}",
                             width="stretch", type="primary"):
                    tracker.record_pick(p.player_key, team_key=str(my_slot))
                    st.cache_data.clear()
                    st.rerun()

            if phone_layout:
                # Full-width button under the text: a bigger tap target.
                _body()
                _button()
            else:
                head, take = st.columns([3, 1])
                with head:
                    _body()
                with take:
                    _button()


def _pane_board(board, drafted, phone_layout):
    st.subheader("Best available")
    positions = ["ALL"] + sorted({p.position for p in board.players})
    chosen = st.radio("Position", positions, horizontal=True, label_visibility="collapsed")
    pool = [p for p in board.available(drafted)
            if chosen == "ALL" or p.position == chosen][:60]

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
            # Columns that do not earn their width on a narrow screen.
            row.update({
                "Tm": p.team,
                "ADP": round(p.adp, 1) if p.adp else None,
                "Bye": p.bye_week,
                "Inj": p.injury_status or "",
            })
        rows.append(row)

    st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
        height=420 if phone_layout else 560,
        column_config={
            "Tier": st.column_config.NumberColumn(width="small"),
            "VORP": st.column_config.ProgressColumn(
                format="%.1f",
                min_value=float(min([r["VORP"] for r in rows], default=0)),
                max_value=float(max([r["VORP"] for r in rows], default=1)),
            ),
        },
    )


def _pane_roster(roster, my_players, slots, tracker, board):
    st.subheader("My roster")
    unfilled = roster.unfilled()
    open_slots = {k: v for k, v in unfilled.items() if v > 0}
    if open_slots:
        st.caption("Still needed: " + ", ".join(
            f"{k} {v:.1f}" for k, v in sorted(open_slots.items())
        ))
    else:
        st.caption("All starting slots filled.")

    counts = roster.counts()
    tracked = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF")
               if p in counts or p in slots]
    if tracked:
        st.markdown(
            " &nbsp;|&nbsp; ".join(f"**{p}** {counts.get(p, 0)}" for p in tracked),
            unsafe_allow_html=True,
        )

    for p in my_players:
        st.markdown(
            f"<span style='color:{tier_color(p.tier)}'>&#9632;</span> "
            f"**{p.name}** {p.position}{p.position_rank} &middot; "
            f"{p.points:.0f} pts &middot; bye {p.bye_week or '-'}",
            unsafe_allow_html=True,
        )

    byes = roster.bye_weeks()
    stacked = {w: len(v) for w, v in byes.items() if len(v) >= 2}
    if stacked:
        st.warning("Bye stacking: " + ", ".join(
            f"week {w}: {n}" for w, n in sorted(stacked.items())
        ))

    st.divider()
    st.caption("Recent picks")
    for pick in sorted(tracker.state.picks.values(), key=lambda x: -x.pick)[:10]:
        player = board.get(pick.player_key) if pick.player_key else None
        name = player.name if player else (pick.player_key or "?")
        st.caption(f"{pick.pick}. slot {pick.team_key} - {name}")


# --- season view -------------------------------------------------------------

def season_view(cfg, conn, league_key):
    settings = settings_of(conn, league_key)
    slots = starting_slots_of(settings)
    season = int(cfg.get("league.season") or 2026)

    st.subheader("Season dashboard")
    tabs = st.tabs(["Recommendations", "Injuries", "Trending adds", "Board"])

    with tabs[0]:
        rows = conn.execute(
            "SELECT job, week, payload_json, created_at, notified_at "
            "FROM recommendations ORDER BY created_at DESC LIMIT 40"
        ).fetchall()
        if not rows:
            st.info("No jobs have run yet. Try `python fcc.py injuries --dry-run`.")
        for row in rows:
            import json

            payload = json.loads(row["payload_json"])
            with st.expander(
                f"[{row['job']}] {payload.get('title', '')}  -  {row['created_at']}"
            ):
                st.text("\n".join(payload.get("lines", [])))

    with tabs[1]:
        rows = conn.execute(
            """
            SELECT p.full_name, p.position, p.team, i.status, i.body_part, i.observed_at
            FROM injuries i JOIN players p USING(player_key)
            JOIN (SELECT player_key, MAX(observed_at) latest FROM injuries GROUP BY player_key) m
              ON m.player_key=i.player_key AND m.latest=i.observed_at
            ORDER BY CASE i.status WHEN 'IR' THEN 1 WHEN 'Out' THEN 2
                     WHEN 'Doubtful' THEN 3 ELSE 4 END LIMIT 100
            """
        ).fetchall()
        st.dataframe([dict(r) for r in rows], width="stretch", hide_index=True)

    with tabs[2]:
        rows = conn.execute(
            """
            SELECT p.full_name, p.position, p.team, t.count, t.kind
            FROM trending t JOIN players p USING(player_key)
            WHERE t.kind='add' ORDER BY t.count DESC LIMIT 40
            """
        ).fetchall()
        st.dataframe([dict(r) for r in rows], width="stretch", hide_index=True)

    with tabs[3]:
        board = load_board(conn, season, slots, int(settings.get("num_teams") or 12), 0.08)
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
            width="stretch", hide_index=True, height=600,
        )


def main():
    # Gate first: nothing touches the database or renders league data until the
    # password is accepted. Open automatically when no password is configured.
    auth.require_password()

    cfg, conn, league_key = get_context()
    st.title("Fantasy Command Center")
    mode = st.sidebar.radio("Mode", ["Draft", "Season"])
    if mode == "Draft":
        draft_view(cfg, conn, league_key)
    else:
        season_view(cfg, conn, league_key)


main()
