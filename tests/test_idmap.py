"""Identity resolution tests (spec 7)."""
from __future__ import annotations

import pytest

from src import db
from src.idmap import IdMapper, make_player_key, normalize_name, normalize_position, normalize_team


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def mapper(conn):
    m = IdMapper(conn)
    m.upsert_player(full_name="Ja'Marr Chase", position="WR", team="CIN", yahoo_id="31896")
    m.upsert_player(full_name="Kenneth Walker III", position="RB", team="SEA", yahoo_id="34012")
    m.upsert_player(full_name="D.K. Metcalf", position="WR", team="SEA", yahoo_id="31896x")
    m.upsert_player(full_name="Patrick Mahomes", position="QB", team="KC", yahoo_id="30123")
    m.upsert_player(full_name="San Francisco", position="DEF", team="SF", yahoo_id="100026")
    conn.commit()
    return m


# --- normalization -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("D.K. Metcalf", "dk metcalf"),
        ("Ja'Marr Chase", "jamarr chase"),
        ("Kenneth Walker III", "kenneth walker"),
        ("Michael Pittman Jr.", "michael pittman"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("Marquise Brown", "marquise brown"),
        ("Hollywood Brown", "marquise brown"),      # nickname alias
        ("Mitch Trubisky", "mitchell trubisky"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_team_and_position():
    assert normalize_team("JAC") == "JAX"
    assert normalize_team("wsh") == "WAS"
    assert normalize_position("PK") == "K"
    assert normalize_position("D/ST") == "DEF"


def test_player_key_is_stable_across_team_changes():
    """A trade must not change a player identity."""
    assert make_player_key("Davante Adams", "WR", "LV") == make_player_key(
        "Davante Adams", "WR", "NYJ"
    )


def test_defense_key_uses_team_not_name():
    """Sources name defenses inconsistently; the team is the stable part."""
    assert (
        make_player_key("San Francisco 49ers", "DEF", "SF")
        == make_player_key("49ers", "DST", "SF")
        == "DEF|SF"
    )


# --- resolution --------------------------------------------------------------

def test_exact_match(mapper):
    result = mapper.resolve(source="sleeper", source_id="4034", name="Patrick Mahomes",
                            position="QB", team="KC")
    assert result.player_key == "patrick mahomes|QB"
    assert result.method == "exact"


def test_punctuation_variants_match(mapper):
    """Sleeper writes "DK Metcalf"; Yahoo writes "D.K. Metcalf"."""
    result = mapper.resolve(source="sleeper", source_id="5846", name="DK Metcalf",
                            position="WR", team="SEA")
    assert result.player_key == "dk metcalf|WR"
    assert result.method == "exact"


def test_suffix_variants_match(mapper):
    result = mapper.resolve(source="fantasypros", source_id=None, name="Kenneth Walker",
                            position="RB", team="SEA")
    assert result.player_key == "kenneth walker|RB"


def test_team_change_still_resolves(mapper):
    """Stale team data on the incoming record must not break the match."""
    result = mapper.resolve(source="espn", source_id="99", name="Ja'Marr Chase",
                            position="WR", team="FA")
    assert result.player_key == "jamarr chase|WR"


def test_missing_position_falls_back_to_name(mapper):
    result = mapper.resolve(source="espn", source_id="77", name="Patrick Mahomes",
                            position=None, team=None)
    assert result.player_key == "patrick mahomes|QB"
    assert result.method == "alias"


def test_fuzzy_match_on_misspelling(mapper):
    result = mapper.resolve(source="espn", source_id="55", name="Patrick Mahome",
                            position="QB", team="KC")
    assert result.player_key == "patrick mahomes|QB"
    assert result.method == "fuzzy"
    assert result.confidence >= IdMapper.FUZZY_THRESHOLD


def test_unrelated_name_is_rejected_not_guessed(mapper):
    """A miss must return unmatched rather than a confident wrong answer."""
    result = mapper.resolve(source="espn", source_id="1", name="Zxqv Nonexistent",
                            position="QB", team="KC")
    assert result.player_key is None
    assert result.method == "unmatched"


def test_fuzzy_never_crosses_positions(mapper):
    """Similar names at different positions must not be conflated."""
    mapper.upsert_player(full_name="Josh Allen", position="QB", team="BUF")
    mapper.upsert_player(full_name="Josh Allen", position="DEF", team="JAX")
    mapper.conn.commit()
    qb = mapper.resolve(source="espn", source_id="a1", name="Josh Allen", position="QB")
    assert qb.player_key == "josh allen|QB"


def test_resolution_is_cached_in_id_map(mapper):
    mapper.resolve(source="sleeper", source_id="4034", name="Patrick Mahomes",
                   position="QB", team="KC")
    mapper.conn.commit()
    row = mapper.conn.execute(
        "SELECT player_key FROM player_id_map WHERE source='sleeper' AND source_id='4034'"
    ).fetchone()
    assert row["player_key"] == "patrick mahomes|QB"

    # A later call with a garbled name still resolves via the cached id.
    again = mapper.resolve(source="sleeper", source_id="4034", name="P. Mahomes", position="QB")
    assert again.player_key == "patrick mahomes|QB"


def test_manual_override_wins(conn):
    """The escape hatch for players fuzzy matching gets wrong."""
    m = IdMapper(conn)
    m.upsert_player(full_name="Chris Rodriguez", position="RB", team="WAS")
    conn.commit()

    overrides = {"fantasypros": {"9999": "chris rodriguez|RB"}}
    m.overrides = overrides
    result = m.resolve(source="fantasypros", source_id="9999", name="C. Rodriguez Jr",
                       position="RB", team="WAS")
    assert result.player_key == "chris rodriguez|RB"
    assert result.method == "manual"


def test_upsert_merges_ids_without_erasing(mapper):
    """Sleeper filling in its id must not wipe the Yahoo id."""
    mapper.upsert_player(full_name="Patrick Mahomes", position="QB", team="KC",
                         sleeper_id="4046")
    mapper.conn.commit()
    row = mapper.conn.execute(
        "SELECT yahoo_id, sleeper_id FROM players WHERE player_key='patrick mahomes|QB'"
    ).fetchone()
    assert row["yahoo_id"] == "30123"
    assert row["sleeper_id"] == "4046"


def test_unmatched_report_lists_failures(mapper):
    records = [
        {"id": "1", "name": "Patrick Mahomes", "position": "QB", "team": "KC"},
        {"id": "2", "name": "Totally Unknown Person", "position": "WR", "team": "NYJ"},
    ]
    misses = mapper.unmatched_report(records, source="espn")
    assert len(misses) == 1
    assert misses[0]["name"] == "Totally Unknown Person"
