"""The season pages are built from plain rows that can be checked without a
browser. Rendering is Streamlit's; what goes INTO the tables is ours."""
from __future__ import annotations

import json

from src.analytics.distributions import PlayerForecast
from src.dashboard_season import bench_strength_rows, roster_rows


def test_roster_rows_merge_forecast_tag_slot_and_consensus_context():
    forecasts = [PlayerForecast("dak prescott|QB", "Dak Prescott", "QB", "DAL", 18.8, 7.5)]
    slot_of = {"dak prescott|QB": "QB"}
    tags = {"dak prescott|QB": "Q"}
    consensus = {"dak prescott|QB": json.dumps(
        {"ecr": 4.8, "opponent": "vs. TB", "grade": "A", "sd": 1.47})}
    floors = {"dak prescott|QB": (11.3, 26.3)}
    sources = {"dak prescott|QB": 3}
    [row] = roster_rows(forecasts, slot_of, tags, consensus, floors, sources)
    assert row["Slot"] == "QB" and row["Player"] == "Dak Prescott"
    assert row["Opp"] == "vs. TB" and row["Grade"] == "A"
    assert row["Proj"] == 18.8 and row["Range"] == "11-26"
    assert row["Tag"] == "Q" and row["Sources"] == 3
    assert row["ECR"] == 4.8


def test_roster_rows_order_starters_before_bench_and_ir_last():
    f = [PlayerForecast(k, k, "WR", "X", 5.0, 2.0) for k in ("a", "b", "c", "d")]
    slot_of = {"a": "BN", "b": "WR", "c": "IR", "d": "W/R/T"}
    rows = roster_rows(f, slot_of, {}, {}, {}, {})
    assert [r["Slot"] for r in rows] == ["WR", "W/R/T", "BN", "IR"]


def test_bench_strength_names_the_bench_players_a_free_agent_beats():
    bench = [("Jauan Jennings", "WR", 38.0), ("Jake Ferguson", "TE", 70.0)]
    best_free = {"WR": ("Hot Receiver", 55.0), "TE": ("Spare TE", 40.0)}
    rows = bench_strength_rows(bench, best_free)
    weak = next(r for r in rows if r["Bench player"] == "Jauan Jennings")
    assert weak["Best free agent"] == "Hot Receiver" and weak["Verdict"] == "upgrade available"
    fine = next(r for r in rows if r["Bench player"] == "Jake Ferguson")
    assert fine["Verdict"] == "fine"
