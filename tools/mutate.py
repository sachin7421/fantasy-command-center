"""METHOD.md 4: break the code on purpose and see whether anything goes red.

Each mutation is applied, the named check is run, the file is restored, and a
clean `git status` is proved at the end. Two rules from METHOD, both of which
this harness broke on its first run and now enforces:

  1. Refuse a target string that is not unique - a mutation applied to the
     wrong occurrence reports a false all-clear.
  2. Restore exactly, bytes for bytes. Rewriting a CRLF file as LF left the
     tree dirty after a "restore", which is rule 2 failing in the harness.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

PY = [str(Path(".venv/Scripts/python.exe"))]
CRLF = "\r\n"

MUTATIONS = [
    # (label, file, find, replace, command that must FAIL)
    (
        "scoring: the league's interception override removed",
        "src/league_bootstrap.py",
        '(6,  "Interceptions",                "Int",           "O", -1)',
        '(6,  "Interceptions",                "Int",           "O", 0)',
        PY + ["-m", "pytest", "-q", "tests/test_scoring.py", "tests/test_golden.py",
              "tests/test_league_rules.py"],
    ),
    (
        "uncertainty: measured QB sigma replaced with a guess",
        "src/analytics/uncertainty.py", '"QB": 7.07,', '"QB": 9.99,',
        PY + ["-m", "pytest", "-q", "tests/test_uncertainty.py"],
    ),
    (
        "compliance: RLS not enforced on a brand new database",
        "src/schema.py", "        enforce_rls(conn)\n        return ran", "        return ran",
        PY + ["-m", "pytest", "-q", "tests/test_rls.py"],
    ),
    (
        "compliance: RLS not enforced after the baseline",
        "src/schema.py", "    enforce_rls(conn)\n    return ran", "    return ran",
        PY + ["-m", "pytest", "-q", "tests/test_rls.py"],
    ),
    (
        "compliance: a Yahoo table written again",
        "src/yahoo_snapshot.py", "class LeagueSnapshot:",
        'def _leak(conn):\n    conn.execute("INSERT INTO rosters(team_key) VALUES (?)", ("1",))\n\n\nclass LeagueSnapshot:',
        PY + ["tools/check_yahoo_persistence.py"],
    ),
    (
        "degradation: a silent handler smuggled in",
        "src/manual_wire.py", "    resolve = name_resolver(conn)",
        "    try:\n        pass\n    except Exception:\n        pass\n    resolve = name_resolver(conn)",
        PY + ["tools/check_degradation.py"],
    ),
    (
        "wire: the FA/W status line no longer required",
        "src/manual_wire.py", "        if not _STATUS.match(status):\n            continue\n", "",
        PY + ["-m", "pytest", "-q", "tests/test_manual_wire.py"],
    ),
    (
        "waivers: free agents priced as FAAB claims again",
        "src/season/waivers.py", "        if candidate.player_key in snapshot.free_adds:",
        "        if False:",
        PY + ["-m", "pytest", "-q", "tests/test_waiver_wire.py"],
    ),
    (
        "scope check: an outage reported as Yahoo's answer",
        "src/yahoo_client.py", 'return ScopeCheck("unknown", failure)',
        'return ScopeCheck("not-attached", failure)',
        PY + ["-m", "pytest", "-q", "tests/test_yahoo_scope.py"],
    ),
    (
        "gate: a checker that reads nothing reports success",
        "tools/check_degradation.py", "    if files is None:\n        return 1",
        "    if files is None:\n        files = []",
        PY + ["-m", "pytest", "-q", "tests/test_gates.py"],
    ),
]


def _porcelain() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
    ).stdout.strip()


def main() -> int:
    survivors: list[str] = []
    # Compared against how the tree started, so uncommitted work in progress is
    # not mistaken for a mutation this harness failed to restore.
    baseline = _porcelain()
    for label, path_str, find, replace, command in MUTATIONS:
        path = Path(path_str)
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        flat = text.replace(CRLF, "\n")
        count = flat.count(find)
        if count != 1:
            print(f"REFUSED  {label}: target appears {count} times in {path_str}")
            survivors.append(label)
            continue
        mutated = flat.replace(find, replace)
        if CRLF in text:
            mutated = mutated.replace("\n", CRLF)
        try:
            path.write_bytes(mutated.encode("utf-8"))
            code = subprocess.run(
                command, capture_output=True, text=True, check=False
            ).returncode
        finally:
            path.write_bytes(raw)
        if code == 0:
            print(f"SURVIVED {label}  <- nothing noticed")
            survivors.append(label)
        else:
            print(f"caught   {label}")

    dirty = "" if _porcelain() == baseline else _porcelain()
    print()
    print(f"{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations caught")
    print("tree restored" if not dirty else f"TREE NOT RESTORED:\n{dirty}")
    return 1 if survivors or dirty else 0


if __name__ == "__main__":
    raise SystemExit(main())
