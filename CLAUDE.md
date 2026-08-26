# Engineering Standards — non-negotiable

1. **Test-first**: for every function with logic in it, write the test first, run it
   to confirm it fails, then implement until it passes.
2. **Verify before claiming done**: after every change, run the full test suite, the
   linter (ruff), and type checker (mypy). Never say a task is complete without
   pasting the actual passing output. "It should work" is banned — only "I ran it and
   here is the output."
3. **Never guess an API**: if unsure of any library function signature, endpoint, or
   response shape (especially yfpy, Sleeper, nflverse), read the installed package
   source or fetch the real docs first. If still unsure, write a tiny probe script,
   run it, and inspect the real response before building on it.
4. **No silent failures**: every caught exception is logged or re-raised with context.
   External API calls get timeouts, retries, and a graceful fallback to cached data
   with a warning.
5. **Boring code wins**: readable, typed (full type hints), small functions, no
   cleverness. Docstrings state what and why.
6. **Bugs get a regression test**: when any bug is found, first write a failing test
   that reproduces it, then fix it, then show both.
7. **Real data early**: test against real API responses (cached as fixtures) rather
   than only invented mocks. Mocks lie.
8. **Small verified increments**: never write more than ~150 lines without stopping to
   run something.

---

# Working protocol

**A. One phase at a time.** Before writing any code for a phase, present a short plan:
files to create/change, key design decisions, what could go wrong, and how correctness
will be verified. **Wait for approval before coding.**

**B. Definition of done** for each phase: all tests green, lint and mypy clean, the
phase's CLI command actually executed with real output shown, and a 3-line summary of
what was verified.

**C. Review pass after approval.** Re-read the diff as a skeptical staff engineer
looking for edge cases, error-handling gaps, ID-mapping mistakes, and guessed APIs.
Fix what is found and show what changed.

**D. The scoring engine is the foundation.** Its acceptance test — reproducing Yahoo's
displayed weekly points for 10 real players — must pass before any downstream feature
(VORP, draft board, waivers) is built.

**E. Blocked data source: do not fake it.** Say so, implement the CSV-import fallback
from the spec, and continue.

**F. Track progress in `PROGRESS.md`** so any future session can resume exactly where
we left off.

---

# How these are enforced here

Standards 2, 4, 5 and 6 are not left to memory — they are gates:

```
python tools/gate.py          # all six checks, ~17s
git config core.hooksPath .githooks   # once per clone; runs the gate on push
```

| Standard | Enforced by |
|---|---|
| 2. Verify before done | `tools/gate.py` — ruff, mypy, pytest, degradation, vulture, bandit. CI runs the identical list. |
| 4. No silent failures | `tools/check_degradation.py` — a broad `except` must raise, log, tell the user, or return the error as a value. Legitimate exceptions are declared in place with `# silent: <reason>`. |
| 5. Full type hints | mypy gates at zero errors; `continue-on-error` is off. |
| 6. Regression test per bug | `tests/test_invariants.py` — after fixing a bug, assert the property that was false while it was broken. |
| — behaviour drift | `tests/test_golden.py` — every number the model produces is frozen in `tests/golden/*.json`. |

See `CONTRIBUTING.md` for why each gate exists and which real bug motivated it.

**Standard 3 has a live exception worth knowing:** Yahoo API access is not yet
approved, so `yfpy` response shapes are modelled from documentation rather than
observed. `src/league_bootstrap.py` holds settings transcribed by hand from the league
settings page. This is the CSV-fallback path of protocol E, and it is labelled as a
bootstrap, not a source of truth.
