"""Ranking a streamable position for one week.

A no-kicker league has two slots you can realistically churn: defence and
tight end. Nothing in the app addressed either - the waiver report ranks by
rest-of-season value, which is the wrong question for a slot you intend to
change again next week.

What this can and cannot do is the interesting part. Without Yahoo we do not
know who is AVAILABLE, so it cannot say "pick up this free agent". It can say
where the defence you own sits among all 32 this week, which is the signal
that sends you to look - and it says so rather than implying it knows the
wire.
"""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper

SEASON, WEEK, LEAGUE = 2026, 3, "nfl.l.796511"


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "stream.db", force_sqlite=True)
    idmap = IdMapper(connection)
    for name, points in [
        ("Alpha Defence", 11.0), ("Bravo Defence", 9.5), ("Charlie Defence", 8.0),
        ("Delta Defence", 6.5), ("Echo Defence", 3.0),
    ]:
        key = idmap.upsert_player(full_name=name, position="DEF", team=name[:3].upper())
        connection.execute(
            "INSERT INTO projections_blended(player_key, season, week, points, "
            "computed_at) VALUES (?,?,?,?,?)",
            (key, SEASON, WEEK, points, db.utcnow()),
        )
    connection.commit()
    yield connection
    connection.close()


def test_it_ranks_the_position_for_that_week(conn):
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine=set())
    assert [o.name for o in report.options][:3] == [
        "Alpha Defence", "Bravo Defence", "Charlie Defence"
    ]
    assert report.options[0].points == pytest.approx(11.0)


def test_it_says_where_the_one_you_own_sits(conn):
    """The decision-relevant fact, and the only one it actually knows."""
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine={"DEF|DEL"})
    assert report.my_rank == 4
    assert report.my_option.name == "Delta Defence"
    assert report.field_size == 5
    # 11.0 best against 6.5 owned.
    assert report.gain == pytest.approx(4.5)


def test_owning_the_best_one_is_reported_as_no_gain(conn):
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine={"DEF|ALP"})
    assert report.my_rank == 1
    assert report.gain == pytest.approx(0.0)


def test_owning_none_is_not_a_crash(conn):
    """Before a roster is entered, this still answers the general question."""
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine=set())
    assert report.my_rank is None
    assert report.my_option is None
    assert report.gain is None
    assert report.options


def test_a_week_with_no_projections_says_so_rather_than_ranking_nothing(conn):
    """An empty ranking reads as "no good options", which is a lie."""
    from src.season.streamers import rank

    report = rank(conn, SEASON, 17, "DEF", mine=set())
    assert report.options == []
    assert report.has_data is False


def test_the_report_states_that_availability_is_unknown(conn):
    """It must not imply it knows who is free.

    Every other ranking in this app is over players it knows are available.
    This one is over the whole league, because without Yahoo the wire is
    invisible - and presenting it as a pickup list would be inventing
    knowledge.
    """
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine={"DEF|DEL"})
    assert "availab" in report.caveat.lower()


def test_the_field_size_is_the_whole_league_not_the_shown_list(conn):
    """"2 of 12" when 32 defences exist is a different claim entirely.

    field_size was the length of the TRUNCATED options list, so a top-12
    display made every ranking read as though the league had twelve teams.
    Rank 2 of 32 and rank 2 of 12 are not the same fact.
    """
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine={"DEF|DEL"}, limit=2)
    assert len(report.options) <= 3          # two shown, plus mine kept
    assert report.field_size == 5, "field_size shrank to the display limit"
    assert "of 5" in " ".join(report.describe())


def test_each_option_carries_its_own_rank(conn):
    """Owning two at a position showed the second one the FIRST one's rank.

    With Tyler Warren 4th and Jake Ferguson 20th, the listing printed Ferguson
    as "4" - because the rank came off the report rather than off the player.
    A bench tight end presented as the fourth best in the league is a reason
    to start him.
    """
    from src.season.streamers import rank

    report = rank(conn, SEASON, WEEK, "DEF", mine={"DEF|BRA", "DEF|ECH"})
    by_name = {o.name: o for o in report.options}
    assert by_name["Bravo Defence"].rank == 2
    assert by_name["Echo Defence"].rank == 5
    # my_rank stays the BEST one owned, which is what the summary is about.
    assert report.my_rank == 2
    assert report.my_option.name == "Bravo Defence"
