"""Verified league settings, captured before API access is wired up.

These values were read directly from the league's own Yahoo settings page
(Extra Fun League, ID 796511) on 2026-08-23. They are shaped exactly like the
payload `YahooClient.fetch_league_settings()` returns, so the scoring engine,
VORP board and every job can run today and switch to the live API with no code
change.

This is a BOOTSTRAP, not a second source of truth: once OAuth is configured,
`fcc sync-settings` overwrites this row from the API and the league_settings
table stays authoritative. Use `fcc verify-settings` to diff the two.
"""
from __future__ import annotations

import json

from src import db
from src.storage import Database

LEAGUE_ID = "796511"
LEAGUE_NAME = "Extra Fun League"
SEASON = 2026
DRAFT_TIME_LOCAL = "Tue Sep 8 2026 8:30pm EDT"

# (stat_id, name, display_name, position_type, modifier)
# stat_ids follow Yahoo's standard NFL numbering so that, when the live API
# replaces this, ids line up and the diff is meaningful.
_OFFENSE = [
    (4,  "Passing Yards",                "Pass Yds",      "O", 0.04),   # 25 yds/pt
    (5,  "Passing Touchdowns",           "Pass TD",       "O", 4),
    (6,  "Interceptions",                "Int",           "O", -1),     # league override
    (9,  "Rushing Yards",                "Rush Yds",      "O", 0.1),    # 10 yds/pt
    (10, "Rushing Touchdowns",           "Rush TD",       "O", 6),
    (11, "Receptions",                   "Rec",           "O", 0.5),    # half PPR
    (12, "Reception Yards",              "Rec Yds",       "O", 0.1),    # 10 yds/pt
    (13, "Reception Touchdowns",         "Rec TD",        "O", 6),
    (15, "Return Touchdowns",            "Ret TD",        "O", 6),
    (16, "2-Point Conversions",          "2-PT",          "O", 2),
    (17, "Fumbles",                      "Fum",           "O", -1),     # league override
    (18, "Fumbles Lost",                 "Fum Lost",      "O", -1),     # league override
    (57, "Offensive Fumble Return TD",   "Off Fum Ret TD", "O", 6),
]

_DEFENSE = [
    (32, "Sack",                              "Sack",          "DT", 1),
    (33, "Interception",                      "Int",           "DT", 2),
    (34, "Fumble Recovery",                   "Fum Rec",       "DT", 2),
    (35, "Touchdown",                         "TD",            "DT", 6),
    (36, "Safety",                            "Safe",          "DT", 2),
    (37, "Block Kick",                        "Blk Kick",      "DT", 2),
    (38, "Kickoff and Punt Return Touchdowns", "Ret TD",       "DT", 6),
    (49, "Extra Point Returned",              "XPR",           "DT", 2),
    (50, "Points Allowed 0 points",           "Pts Allow 0",     "DT", 10),
    (51, "Points Allowed 1-6 points",         "Pts Allow 1-6",   "DT", 7),
    (52, "Points Allowed 7-13 points",        "Pts Allow 7-13",  "DT", 4),
    (53, "Points Allowed 14-20 points",       "Pts Allow 14-20", "DT", 1),
    (54, "Points Allowed 21-27 points",       "Pts Allow 21-27", "DT", 0),
    (55, "Points Allowed 28-34 points",       "Pts Allow 28-34", "DT", -1),
    (56, "Points Allowed 35+ points",         "Pts Allow 35+",   "DT", -4),
]

_ALL = _OFFENSE + _DEFENSE

# QB, WR, WR, RB, RB, TE, W/R/T, W/R/T, DEF, BN x5, IR x2.
# Note: this league has NO kicker slot, so kickers are not draftable at all.
ROSTER_POSITIONS = [
    ("QB", 1), ("WR", 2), ("RB", 2), ("TE", 1), ("W/R/T", 2), ("DEF", 1),
    ("BN", 5), ("IR", 2),
]


#: How this league actually behaved in 2025, read off the standings page on
#: 2026-08-25. Recorded because the FAAB model's only alternative is a guess
#: until Yahoo access lands and the transaction log can be parsed properly.
#:
#:   team                       budget left   waiver moves
#:   NUB                              $100              6
#:   Fusballers                        $73             11
#:   Butt Fumblers                     $50             15
#:   Galloping Gandhi                  $38             29
#:   STFU Y!                           $35             24
#:   Ehcanadiantuxedo                  $32             24
#:   Problematic Team Mascot           $25             26
#:   Aaryan's Lamarvelous Team          $7             35
#:   Pipelayers                         $4             17
#:   Frustration Makes You Stronger     $3             21
#:   BIG PENIX!                         $0             50
#:   Dirties                            $0             32
#:
#: Two things worth knowing from it. The league SPENDS: median remaining budget
#: was $28, so a typical manager used about 70% of it, and two finished at zero.
#: A bid model that assumes people hoard would be wrong here.
#:
#: And Butt Fumblers is passive on waivers by this league's standards - 15
#: moves against a median of 24, ending with half the budget unspent, while
#: scoring the most points in the league and finishing 10-4. That is money left
#: on the table.
#:
#: Three teams have since been renamed, so these do not all map onto the 2026
#: team ids by name alone.
LEAGUE_2025_BUDGET_LEFT = {
    "NUB": 100, "Fusballers": 73, "Butt Fumblers": 50, "Galloping Gandhi": 38,
    "STFU Y!": 35, "Ehcanadiantuxedo": 32, "Problematic Team Mascot": 25,
    "Aaryan's Lamarvelous Team": 7, "Pipelayers": 4,
    "Frustration Makes You Stronger": 3, "BIG PENIX!": 0, "Dirties": 0,
}
LEAGUE_2025_WAIVER_MOVES = {
    "BIG PENIX!": 50, "Aaryan's Lamarvelous Team": 35, "Dirties": 32,
    "Galloping Gandhi": 29, "Problematic Team Mascot": 26, "STFU Y!": 24,
    "Ehcanadiantuxedo": 24, "Frustration Makes You Stronger": 21,
    "Pipelayers": 17, "Butt Fumblers": 15, "Fusballers": 11, "NUB": 6,
}


def build_settings() -> dict:
    """A Yahoo-shaped league settings payload."""
    return {
        "league_id": LEAGUE_ID,
        "name": LEAGUE_NAME,
        "season": SEASON,
        "num_teams": 12,
        "scoring_type": "head",
        "draft_type": "live",
        "is_auction_draft": "0",
        "draft_time": DRAFT_TIME_LOCAL,
        "uses_faab": "1",
        "faab_budget": 100,          # Yahoo default; confirm on the league page
        "waiver_type": "FR",         # FAB with continual rolling-list tiebreak
        "waiver_rule": "gametime",
        "waiver_time": "1",
        "trade_end_date": "2026-11-28",
        "playoff_start_week": 15,
        "num_playoff_teams": 6,
        "max_acquisitions_season": 75,
        # Both verified against the live Yahoo settings page on 2026-08-25.
        #
        # This league does NOT reseed between playoff rounds, so the bracket is
        # fixed once the field is set: the 1 seed plays the winner of 4-v-5
        # whatever happens elsewhere. Reseeding would hand the top seed the
        # weakest survivor instead, which is a materially different set of
        # title odds.
        "playoff_reseeding": 0,
        # 1 minute 30 seconds per pick - the reason the draft board is built to
        # be read at a glance rather than studied.
        "draft_pick_time": 90,
        "fractional_points": "1",
        "negative_points": "1",
        "roster_positions": [
            {"roster_position": {"position": pos, "count": count}}
            for pos, count in ROSTER_POSITIONS
        ],
        "stat_categories": {
            "stats": [
                {
                    "stat": {
                        "stat_id": sid,
                        "name": name,
                        "display_name": display,
                        "position_type": ptype,
                        "is_only_display_stat": 0,
                    }
                }
                for sid, name, display, ptype, _ in _ALL
            ]
        },
        "stat_modifiers": {
            "stats": [
                {"stat": {"stat_id": sid, "value": value}}
                for sid, _, _, _, value in _ALL
            ]
        },
    }


def install(conn: Database, league_key: str | None = None) -> str:
    """Store the bootstrap settings if nothing is stored yet.

    Never overwrites settings that came from the live API.
    """
    key = league_key or f"nfl.l.{LEAGUE_ID}"
    row = conn.execute(
        "SELECT settings_json FROM league_settings WHERE league_key=?", (key,)
    ).fetchone()
    if row is not None:
        return key
    conn.execute(
        "INSERT INTO league_settings(league_key, season, settings_json, fetched_at) "
        "VALUES (?,?,?,?)",
        (key, SEASON, json.dumps(build_settings()), db.utcnow()),
    )
    conn.commit()
    return key


def starting_slots() -> dict[str, int]:
    return {
        pos: count for pos, count in ROSTER_POSITIONS if pos not in ("BN", "IR")
    }
