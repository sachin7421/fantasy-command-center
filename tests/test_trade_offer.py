"""Evaluating a trade somebody offered you.

`fcc trades` proposes its own one-for-one ideas. That is not the request a
manager actually gets - which is "Dave offered me Kyren Williams for Puka
Nacua and a bench back, is that good?"

It needs the other team's roster, which cannot be fetched without Yahoo and
does not need to be: you can paste it, the same way you paste your own.

The measure is the one the waiver job settled on - what the trade does to your
STARTING LINEUP, not to the sum of the players. A trade that upgrades your
third receiver while costing you a starting back is a downgrade, however the
raw points read.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper

SEASON, WEEK = 2026, 3
SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 2, "DEF": 1}


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offer.db", force_sqlite=True)
    idmap = IdMapper(connection)
    roster = [
        ("My QB", "QB", 300.0), ("Stud RB", "RB", 260.0), ("Good RB", "RB", 210.0),
        ("Spare RB", "RB", 90.0), ("Stud WR", "WR", 250.0), ("Good WR", "WR", 200.0),
        ("Weak WR", "WR", 95.0), ("My TE", "TE", 150.0), ("My DEF", "DEF", 110.0),
        ("Their RB", "RB", 255.0), ("Their WR", "WR", 130.0),
    ]
    for name, position, points in roster:
        key = idmap.upsert_player(full_name=name, position=position, team="AAA")
        connection.execute(
            "INSERT INTO projections_blended(player_key, season, week, points, "
            "computed_at) VALUES (?,?,?,?,?)",
            (key, SEASON, 0, points, db.utcnow()),
        )
    connection.commit()
    yield connection
    connection.close()


MINE = ["My QB", "Stud RB", "Good RB", "Spare RB", "Stud WR",
        "Good WR", "Weak WR", "My TE", "My DEF"]


def test_a_fair_swap_of_starters_is_close_to_neutral(conn):
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Good RB"], i_get=["Their RB"],
    )
    # Their RB (255) for my Good RB (210) is a clear upgrade.
    assert verdict.my_gain > 0
    assert verdict.accept is True


def test_giving_up_a_starter_for_a_bench_player_is_refused(conn):
    """The point of the whole thing: raw points are not the measure."""
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Stud RB"], i_get=["Their WR"],
    )
    assert verdict.my_gain < 0
    assert verdict.accept is False
    assert "lineup" in " ".join(verdict.reasons).lower()


def test_an_unmatched_name_is_refused_rather_than_guessed(conn):
    """Half a trade evaluated is worse than none.

    Silently dropping a name the app does not recognise values the trade as
    though that player were not in it - which is exactly how you accept a
    two-for-one that you read as a one-for-one.
    """
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Stud RB"], i_get=["Nobody At All"],
    )
    assert verdict.unmatched == ["Nobody At All"]
    assert verdict.accept is None, "a trade it cannot read must not get a verdict"


def test_a_two_for_one_accounts_for_the_roster_spot(conn):
    """Two in and one out leaves you a man over; one in and two out, a man short.

    Ignoring that made every two-for-one look good: you counted both incoming
    players and never counted the bench spot they have to come from.
    """
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Good RB"], i_get=["Their RB", "Their WR"],
    )
    assert verdict.roster_change == 1
    assert any("roster" in r.lower() for r in verdict.reasons)


def test_it_values_both_sides_not_just_yours(conn):
    """A trade the other manager will never accept is not an opportunity."""
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Weak WR"], i_get=["Their RB"],
        their_roster=["Their RB", "Their WR"],
    )
    assert verdict.their_gain is not None
    assert verdict.their_gain < 0, "they are giving up their best player for nothing"


def test_receiving_a_player_you_already_own_is_rejected(conn):
    """You cannot trade for a player who is already yours.

    Without this the incoming duplicate is appended to the roster and the
    lineup solver can start the SAME player in two slots - an impossible team,
    valued confidently. It is an easy thing to type when reading an offer off
    a screen, so it has to be caught rather than computed.
    """
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Weak WR"], i_get=["Stud RB"],
    )
    assert verdict.accept is None
    assert any("already" in r.lower() for r in verdict.describe())


def test_sending_a_player_you_do_not_own_is_rejected(conn):
    """The mirror: you cannot give away someone who is not yours."""
    from src.season.trade_offer import evaluate

    verdict = evaluate(
        conn, SEASON, WEEK, SLOTS,
        my_roster=MINE, i_give=["Their RB"], i_get=["Their WR"],
    )
    assert verdict.accept is None
    assert any("not on your roster" in r.lower() for r in verdict.describe())
