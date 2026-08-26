"""Every check, one command, same order as CI.

    python tools/gate.py            # the full gate, stops at the first failure
    python tools/gate.py --fast     # tests and lint only (~15s), for mid-edit
    python tools/gate.py --no-stop  # run everything, report all failures

The reason this file exists is not that the checks were missing. They existed
and they worked. The reason is that running them was OPTIONAL, and depended on
remembering which four commands to type in which order - so a broken f-string
went out because a push happened without them, and CI caught it 47 seconds
later. One command that is trivial to type and impossible to half-run removes
that failure mode entirely, and the pre-push hook removes the remembering.

Order is deliberate: cheapest and most specific first, so the failure you are
shown is the one closest to what you just typed.
"""
from __future__ import annotations

import subprocess
import sys
import time

PY = [sys.executable]

#: (label, argv, what a failure here means)
CHECKS: list[tuple[str, list[str], str]] = [
    (
        "lint",
        PY + ["-m", "ruff", "check", "src", "dashboard.py", "fcc.py", "tests", "tools"],
        "style and obvious bugs",
    ),
    (
        "types",
        PY + ["-m", "mypy", "src", "dashboard.py", "fcc.py"],
        "a caller and the thing it calls have drifted apart",
    ),
    (
        "tests",
        PY + ["-m", "pytest", "tests", "-q"],
        "behaviour, invariants and the golden snapshots",
    ),
    (
        "degradation",
        PY + ["tools/check_degradation.py"],
        "a failure is being swallowed without a word to anyone",
    ),
    (
        "dead code",
        PY + ["-m", "vulture", "src", "dashboard.py", "fcc.py", "--min-confidence", "80"],
        "code written and never connected to anything",
    ),
    (
        "security",
        PY + ["-m", "bandit", "-r", "src", "-q", "-ll", "-c", ".bandit"],
        "an unsafe call",
    ),
]

FAST = {"lint", "types", "tests"}


def run(label: str, argv: list[str]) -> tuple[bool, float, str]:
    started = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)
    elapsed = time.monotonic() - started
    return proc.returncode == 0, elapsed, (proc.stdout or "") + (proc.stderr or "")


def main(argv: list[str]) -> int:
    fast = "--fast" in argv
    keep_going = "--no-stop" in argv
    checks = [c for c in CHECKS if not fast or c[0] in FAST]

    failed: list[tuple[str, str, str]] = []
    for label, command, meaning in checks:
        print(f"  {label:<12} ...", end="", flush=True)
        ok, elapsed, output = run(label, command)
        if ok:
            print(f"\r  {label:<12} ok    {elapsed:5.1f}s")
            continue
        print(f"\r  {label:<12} FAIL  {elapsed:5.1f}s   ({meaning})")
        failed.append((label, meaning, output))
        if not keep_going:
            break

    if not failed:
        scope = "fast gate" if fast else "gate"
        print(f"\n{scope} passed - safe to push.")
        return 0

    for label, meaning, output in failed:
        print(f"\n{'=' * 70}\n{label}: {meaning}\n{'=' * 70}")
        print(output.strip()[:8000])

    print(f"\n{len(failed)} check(s) failed. Nothing was pushed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
