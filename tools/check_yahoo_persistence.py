"""Fail the build if any code path writes Yahoo Fantasy data to disk.

Obligation 1 of the API agreement signed 2026-09-10. The refactor that removed
every such write is done; this is what stops it coming back.

It exists because the breach is SILENT. Nothing breaks when Yahoo data is
written to a table - the application works perfectly, the tests pass, and the
only symptom is a contract being broken that nobody notices. That is the same
shape as every other bug this project has had to dig out, and the same answer
applies: make it impossible rather than discouraged.

Two things are reported:

    a write to a table that exists only to hold Yahoo league state
    a call to `cache_put` with a Yahoo source

Reads are fine. So is `DROP TABLE rosters` - the migration that removes these
tables has to mention them by name, and a checker that cannot tell a deletion
from an insertion would block its own cleanup.

Run:  python tools/check_yahoo_persistence.py [paths...]
Exit: 0 clean, 1 a Yahoo write was found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

#: Tables that exist only to hold Yahoo league state.
YAHOO_TABLES = ("rosters", "free_agents", "team_budgets", "transactions")

#: Any statement that puts rows INTO one of them. UPDATE and INSERT both
#: persist; SELECT and DROP do not.
_WRITE = re.compile(
    r"\b(INSERT\s+(?:OR\s+REPLACE\s+)?INTO|UPDATE)\s+(" + "|".join(YAHOO_TABLES) + r")\b",
    re.IGNORECASE,
)

#: cache_put(..., "yahoo", ...) in any spelling of the source argument.
_CACHE = re.compile(r"cache_put\s*\([^)]*[\"'][^\"']*yahoo[^\"']*[\"']", re.IGNORECASE)

#: A file may opt out where it is the thing doing the ENFORCING, or where it is
#: deliberately demonstrating the pattern.
MARKER = "# yahoo-persistence-ok:"


def check_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: could not read ({exc})"]

    problems = []
    for number, line in enumerate(source.splitlines(), 1):
        if MARKER in line:
            continue
        match = _WRITE.search(line)
        if match:
            problems.append(
                f"{path}:{number}: writes Yahoo league state to `{match.group(2)}`. "
                "Yahoo data must stay in memory for the duration of a run "
                "(API agreement, obligation 1) - put it on the LeagueSnapshot."
            )
        if _CACHE.search(line):
            problems.append(
                f"{path}:{number}: caches a Yahoo payload to disk. "
                "Hold it on the YahooSession instead."
            )
    return problems


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["src", "dashboard.py", "fcc.py"]
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])

    problems = []
    for path in files:
        problems.extend(check_file(path))

    if problems:
        print(f"{len(problems)} Yahoo persistence violation(s):\n")
        for problem in problems:
            print(f"  {problem}\n")
        print(
            "Each of these breaks the API agreement in a way that nothing else "
            "would ever report: the application keeps working."
        )
        return 1

    print(f"No Yahoo data is persisted by {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
