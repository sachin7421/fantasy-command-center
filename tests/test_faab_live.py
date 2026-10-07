"""What the first live transaction log (7 Oct 2026) showed the FAAB model.

Forty-five winning bids this season, median $2, thirteen of them $0, two
above $22. Every defense ever claimed went for $0. The model priced a
Patriots DEF claim at "$65 to win":

  - only 9 of the 45 bids resolved to a player (by Sleeper's Yahoo-id
    cross-reference alone), none got a value, so every manager fell back to
    the generic $1.20/point prior - a textbook number, not this league's;
  - the model is position-blind, so a defense was priced like a running back.
"""
from __future__ import annotations

from src import db
from src.analytics import faab
from src.analytics.faab import ManagerProfile
from src.idmap import IdMapper


def _txn(bid, name, position, team_abbr, player_id, team="3"):
    """One add transaction, as yfpy serialises it (recorded 7 Oct 2026)."""
    return {
        "type": "add/drop", "status": "successful", "faab_bid": bid,
        "players": [{"player": {
            "player_id": player_id, "player_key": f"470.p.{player_id}",
            "display_position": position, "editorial_team_abbr": team_abbr,
            "name": {"full": name},
            "transaction_data": {"type": "add", "source_type": "waivers",
                                 "destination_team_key": f"470.l.796511.t.{team}"},
        }}],
    }


def test_a_bid_resolves_by_name_and_position_like_a_roster_does(tmp_path):
    conn = db.init_db(tmp_path / "f.db", force_sqlite=True)
    idmap = IdMapper(conn)
    ekeler = idmap.upsert_player(full_name="Austin Ekeler", position="RB", team="WAS")
    jets = idmap.upsert_player(full_name="New York Jets", position="DEF", team="NYJ")
    conn.commit()

    records = faab.parse_bids([
        _txn(1, "Austin Ekeler", "RB", "Was", "30423"),
        _txn(0, "Jets", "DEF", "NYJ", "100020"),
    ])
    assert [r.position for r in records] == ["RB", "DEF"]
    faab.resolve_player_keys(conn, records)
    assert [r.player_key for r in records] == [ekeler, jets]
    conn.close()


def test_the_price_for_a_position_nobody_pays_for_is_what_they_paid():
    """Three defenses claimed, all for $0: the field will not pay $65 for one."""
    records = faab.parse_bids([
        _txn(0, "Jaguars", "DEF", "Jax", "100030"),
        _txn(0, "Jets", "DEF", "NYJ", "100020", team="4"),
        _txn(0, "Bengals", "DEF", "Cin", "100004", team="5"),
        _txn(50, "Hot Receiver", "WR", "KC", "41000", team="6"),
    ])
    assert faab.position_market(records, "DEF") == [0, 0, 0]
    assert faab.position_ceiling(records, "DEF") == 0
    assert faab.position_market(records, "WR") is None, "one bid is not a market"
    assert faab.position_market(records, "TE") is None


def test_the_real_defense_market_prices_a_defense_at_a_few_dollars():
    """The twelve DEF claims of the first live log (7 Oct 2026)."""
    rivals = [ManagerProfile(team_key=str(i), name=f"T{i}", observations=0,
                             beta=1.2, raw_beta=None, mean_bid=0.0, max_bid=0,
                             budget_left=100) for i in range(11)]
    market = [0, 0, 0, 0, 0, 0, 0, 1, 3, 5, 6, 10]
    advice = faab.recommend(value=26.6, my_budget=97, rivals=rivals, weeks_left=13,
                            position_bids=market)
    # 70th percentile of twelve bids (nearest rank: the 9th, $3) plus one.
    assert advice.price_to_win == 4
    assert advice.recommended == 4
    assert advice.min_competitive == 1


def test_a_capped_position_caps_the_recommendation():
    rivals = [ManagerProfile(team_key=str(i), name=f"T{i}", observations=0,
                             beta=1.2, raw_beta=None, mean_bid=0.0, max_bid=0,
                             budget_left=100) for i in range(11)]
    uncapped = faab.recommend(value=26.6, my_budget=97, rivals=rivals, weeks_left=13)
    capped = faab.recommend(value=26.6, my_budget=97, rivals=rivals, weeks_left=13,
                            position_bids=[0, 0, 0])
    assert uncapped.price_to_win > 30, "the generic prior really did say this"
    assert capped.price_to_win == 1
    assert capped.recommended == 1
    assert not capped.walk_away
    assert any("$0-$0" in n for n in capped.notes), capped.notes
