# The gate

One command, before every push:

```
python tools/gate.py          # everything, ~18s
python tools/gate.py --fast   # lint + tests only, for mid-edit
```

Enable the hook once per clone and stop thinking about it:

```
git config core.hooksPath .githooks
```

CI runs the same list in the same order. That is deliberate: a gate that
disagrees with the one on your machine teaches you to ignore whichever is
noisier.

---

## Why these checks, and not more unit tests

280 unit tests were passing on the day three real bugs reached the live app.
More tests of that kind would have caught none of them, because none of the
bugs were in a unit. Each layer below exists for a failure that actually
happened here.

| Layer | File | The failure it exists for |
|---|---|---|
| Lint | `ruff` | A broken f-string reached `main`. |
| Unit + acceptance | `tests/test_*.py` | Ordinary regressions. |
| **Invariants** | `tests/test_invariants.py` | `replacement_levels()` returned 0.0 for every position for weeks. It returned, it had a docstring, it had a caller — and nothing ever asked whether its answer could be right. |
| **Golden snapshots** | `tests/test_golden.py` | A refactor changes every number on the board by a little, breaks no test, and nobody notices until the draft. |
| **Silent degradation** | `tools/check_degradation.py` | A hosted app read an empty local SQLite file and reported "run `fcc sync` first". Three separate bugs, one shape: something failed, a handler decided it was survivable, and the program carried on in a broken state that looked like a working one. |
| Dead code | `vulture` | Code written and wired to nothing. |
| Security | `bandit` | Unsafe calls. |
| Types (not yet gating) | `mypy` | A model refactored and a caller not updated. |

The three middle rows are the ones worth understanding.

## Invariants — "this can never be true"

Not "does this function work" but "could this answer possibly be right". A
probability outside [0,1]. A replacement level of zero. A bid above the budget.
A NaN. An empty recommendation list while players remain.

They run the real pipeline end to end and assert properties of the **output**,
so they survive refactoring — they are the contract, not the implementation.

**The rule for adding one:** after fixing any bug, ask *"what was true of the
output while it was broken, that should never be true of any output?"* That
sentence, as an assert, goes in `test_invariants.py`. The bug is then
unreintroducible in any form, including forms nobody has thought of.

This suite found a real bug on its first run: two filters in the recommender,
each correct alone, could between them remove every remaining player and return
`[]` — which renders as an empty panel that reads as "no good options" rather
than "this is broken", on a 90-second clock.

## Golden snapshots — "this has not changed"

Invariants prove an answer is not absurd. Golden files prove it has not moved.
A bid of $47 becoming $46 violates no invariant, and is exactly what a silently
mis-wired refactor looks like.

Every number the model produces on a fixed league is frozen in
`tests/golden/*.json`. When a change is intended:

```
python -m pytest tests/test_golden.py --update-golden
git diff tests/golden/          # <- read this before committing
```

**That second line is the entire point.** The diff is a plain-text statement of
what your change did to every recommendation, tier, bid and probability in the
application — including the parts you were not thinking about. A tweak to a
shrinkage constant meant only for waivers, that turns out to reorder round one,
shows up there and nowhere else.

Never `--update-golden` to make a red build green. If the diff can't be
explained in a sentence, the change is wrong — not the golden file.

## Silent degradation — "say something"

An exception handler must **raise**, **log**, **tell the user**, or **hand the
error to its caller as a value**. A broad handler that does none of those is
reported.

Legitimately silent handlers exist. Declare them in place:

```python
except Exception:  # silent: closing twice is not an error
    pass
```

The marker is deliberately a nuisance to type. Writing it is a decision that
this failure genuinely does not matter — made once, in the open, next to the
code — instead of a habit of `except Exception: pass` that nobody re-reads.

Only broad handlers (`Exception`, `BaseException`, bare `except:`) are reported
by default; all three shipped bugs were `except Exception`. A narrow
`except (TypeError, ValueError)` around a `float()` names the exact failure it
absorbs and could not absorb a connection error if it tried. `--strict` reports
those too.

## Adding a check

New checks go in `CHECKS` in `tools/gate.py` **and** as a step in
`.github/workflows/ci.yml`, in the same position. Cheapest and most specific
first, so the failure you see is the one closest to what you just typed.

A check that fails the build is itself load-bearing code: if it silently stops
checking, every run goes green and nothing says so. So it gets tests too —
see `tests/test_gates.py`, which asserts the degradation checker catches what
it claims to and does not cry wolf. One of those tests exists because a linter's
most dangerous bug is skipping the file it cannot parse.
