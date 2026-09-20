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

**G. Pause triggers — stop and ask, do not finish the thought first.** Autonomous
does not mean unsupervised on what is hard to undo. State what triggered it, what you
were about to do, what you recommend and the alternatives, then wait.

- **Money.** A new paid service, a tier upgrade, a spend cap raised, billing switched
  on for something free. Supabase image transformations were enabled on a sister
  project without asking; that is the shape of this.
- **Anything that reaches a real person or a third party.** Sending email rather than
  drafting it, anything posted publicly, any write to Yahoo.
- **Anything the Yahoo agreement touches.** Persisting a Yahoo response, a new
  identifier written down, changing what `tools/check_yahoo_persistence.py` allows.
- **Credentials.** Rotating, deleting or regenerating one; adding a repository secret.
- **A decision not already specified** — architectural or product. Surface it with a
  recommendation and the trade-offs rather than guessing.
- **A conflict between two documents**, or between a document and an instruction. Ask
  which is authoritative.
- **The same failure twice.** Do not loop; report.

**H. A check is a claim until you have watched it fail.** Write the violation, run it,
see red. `python tools/mutate.py` does this for ten known-dangerous edits and must stay
at 10/10. Absent evidence is NOT PROVEN and fails — a check that examined an empty set
has proved nothing.

---

# How these are enforced here

Standards 2, 4, 5 and 6 are not left to memory — they are gates:

```
python tools/gate.py          # all seven checks, ~45s
python tools/mutate.py        # do the checks actually catch anything? ~4min
git config core.hooksPath .githooks   # once per clone; runs the gate on push
```

| Standard | Enforced by |
|---|---|
| 2. Verify before done | `tools/gate.py` — ruff, mypy, pytest, degradation, vulture, bandit. CI runs the identical list. |
| 4. No silent failures | `tools/check_degradation.py` — a broad `except` must raise, log, tell the user, or return the error as a value. Legitimate exceptions are declared in place with `# silent: <reason>`. |
| 5. Full type hints | mypy gates at zero errors; `continue-on-error` is off. |
| 6. Regression test per bug | `tests/test_invariants.py` — after fixing a bug, assert the property that was false while it was broken. |
| — behaviour drift | `tests/test_golden.py` — every number the model produces is frozen in `tests/golden/*.json`. |
| — Yahoo persistence | `tools/check_yahoo_persistence.py` — no INSERT or UPDATE against a dropped Yahoo table, no Yahoo payload cached. |
| — the checks themselves | `tools/mutate.py` — ten deliberate defects, each of which must turn something red. A check nobody checks quietly stops working. |
| — a check that read nothing | Both checkers report **NOT PROVEN** and fail when a target matches no file. "0 files, no problems found" was previously a pass. |

See `CONTRIBUTING.md` for why each gate exists and which real bug motivated it.

**Standard 3 has a live exception worth knowing:** the Yahoo agreement was signed and
countersigned on 2026-09-13 and the app is registered, but Yahoo has never attached the
Fantasy Sports scope to it — `scope=fspt-r` is refused with `invalid_scope`, checked
daily by `fcc yahoo-scope`. So no Yahoo response has ever been observed: `yfpy` shapes
are modelled from documentation, and `src/league_bootstrap.py` holds settings
transcribed by hand from the league settings page. That file is the LIVE scoring
configuration, not a stale bootstrap — `config.yaml` has no scoring section — and
`tests/test_league_rules.py` pins what it is worth. This is the fallback path of
protocol E.
