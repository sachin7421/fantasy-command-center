"""Operations the METHOD audit found missing: a backup, and a check that the
hosted dashboard is actually up after a deploy."""
from __future__ import annotations

import csv
import gzip
import json

from src import db
from src.idmap import IdMapper
from tools import backup, check_deploy


def test_backup_writes_every_table_and_a_manifest_with_row_counts(tmp_path):
    conn = db.init_db(tmp_path / "b.db", force_sqlite=True)
    idmap = IdMapper(conn)
    idmap.upsert_player(full_name="Dak Prescott", position="QB", team="DAL")
    idmap.upsert_player(full_name="Puka Nacua", position="WR", team="LAR")
    conn.commit()

    out = tmp_path / "out"
    manifest = backup.dump(conn, out)
    assert manifest["tables"]["players"] == 2
    assert (out / "players.csv.gz").exists()
    with gzip.open(out / "players.csv.gz", "rt", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["full_name"] for r in rows} == {"Dak Prescott", "Puka Nacua"}
    written = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert written["tables"] == manifest["tables"]
    assert "my_roster" in manifest["tables"] and "job_runs" in manifest["tables"]
    conn.close()


def test_backup_refuses_yahoo_payload_tables_by_name(tmp_path):
    """Nothing Yahoo-derived is stored, so nothing Yahoo-derived can be backed
    up - and if a table ever appears with a Yahoo-shaped name, the backup
    must stop rather than copy it somewhere else."""
    assert backup.forbidden("yahoo_rosters") and backup.forbidden("league_settings_yahoo")
    assert not backup.forbidden("my_roster") and not backup.forbidden("players")


def test_deploy_check_passes_on_a_healthy_app_and_fails_otherwise():
    calls = []

    def ok(url, timeout):
        calls.append(url)
        return 200, "ok"

    def down(url, timeout):
        return 502, "Bad Gateway"

    assert check_deploy.check("https://x.streamlit.app", get=ok) == (True, "healthy (200 ok)")
    assert calls == ["https://x.streamlit.app/healthz"]
    passed, text = check_deploy.check("https://x.streamlit.app", get=down)
    assert not passed and "502" in text
