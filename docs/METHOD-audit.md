# METHOD.md, applied here — 2026-09-20

An audit of this repository against `METHOD.md`: what it already satisfied, what
it did not, and what is deliberately not done. Written because METHOD's own
advice is to list the shortcuts while you still remember them, and because a
list that is never written is re-derived from scratch by the next session.

This project is unambiguously **production mode** by METHOD's test: it spends
money per run (Supabase, hosted Streamlit), it holds a signed third-party
agreement, it emails a real person, and nobody is willing to delete it.

---

## Already satisfied before today

- **One command that means "done"** — `tools/gate.py`, seven checks, and the
  identical list in CI. Nothing is called finished without it.
- **Evidence, not assertion** — standard 2 of `CLAUDE.md`, enforced by habit and
  by the gate.
- **The checkers have their own tests** — `tests/test_gates.py`, which is
  METHOD 3.5 and the reason today's findings were fixable in minutes.
- **A derived list, not a retyped one** — the guarded Yahoo tables are compared
  against the `DROP TABLE` statements in `src/migrations` (METHOD 3.1).
- **One regression test per bug** — `tests/test_invariants.py`, and the practice
  of asserting the property that was false while the bug lived.
- **An append-only event log** — `job_runs` and `recommendations`; every run
  leaves evidence whether or not it found anything to say.
- **Say what is not covered** — `PROGRESS.md` carries a "known-weak claims"
  section, including the circular benchmarks and the in-sample constants.

## Found and fixed today

Each was found by running METHOD 4 — breaking the code on purpose — rather than
by reading it.

| Finding | Why it was invisible |
|---|---|
| Both gate checkers passed over an **empty file set** (`07efa65`) | "No undeclared silent handlers in 0 files" exits 0. A renamed directory or a wrong working directory disables the check permanently, and it stays green. |
| `enforce_rls` could be deleted from **both** call sites with every RLS test green (`af32ba4`) | Every test called the function directly. The function was covered; the wiring that makes it run on each schema apply — the whole mechanism — was not. |
| The league's **interception override** could be zeroed with the scoring and golden suites green (`16bee1e`) | Both build rules from a conftest fixture, which tests the engine and says nothing about the rules this league is scored by. `league_bootstrap.build_settings()` is the live configuration. |
| The mutation harness itself broke both of METHOD 4's rules on its first run (`5bb98d6`) | It rewrote CRLF files as LF, so "restored" left the tree dirty; and it had no uniqueness check on the string it replaced. |

`python tools/mutate.py` now reports **10/10 caught, tree restored**.

## Open, in priority order

1. ~~**Nothing notices a job that did not run.**~~ **Closed 2026-09-20**
   (`30a8f99`). Every run now ends with `fcc catchup`, and a job it cannot
   recover becomes a notification. The original diagnosis was wrong in one
   detail and worth recording: the Sunday lineup run was not dropped, it
   arrived 3h05m late — 35 minutes after kickoff. Measured over twenty runs,
   GitHub's scheduler is late every time, 2–4 hours typically and 5h30m at
   worst. **Still open, and now separable: the cron times assume punctuality
   they never get.** Moving the lineup slots about three hours earlier would
   put them before kickoff even when late, and must keep the Thursday-to-Sunday
   gap above the 72-hour dedup window. That is a decision, not a fix.
2. **No backup, and nothing that would read its failures.** Supabase holds
   every projection, actual and recommendation this project has produced. There
   is no export, scheduled or otherwise.
3. **No post-deploy check against the real dashboard URL.** The gate only ever
   runs against a local process; hosting behaviour (secrets, auth gate, cold
   start) is a property of the deployment.
4. **The product has never been walked end to end in season mode.** The draft
   flow was; the weekly flow has only been exercised a command at a time.
   METHOD rates this the highest-yield review of all.
5. **`PROGRESS.md` numbers are hand-maintained.** The file says so, which is
   better than not, but METHOD 3.1 says a number a human must remember to update
   is already wrong. A `--counts` flag deriving them would close it.

## Deliberately not done

- **A separate static/runtime check tier.** This project has one database and
  one user; the split earns nothing yet.
- **An owner key on every table.** Single-tenant by design. RLS is on with no
  policy, which is the correct end state here, and the app connects as a role
  that bypasses it.
- **Soft delete everywhere.** `my_roster` is deliberately replace-on-write: a
  re-paste is a correction, and appending would leave dropped players on the
  team. History that matters (projections, actuals, recommendations) is already
  append-only.
- **A spend wrapper.** Nothing here calls a paid API per request. The costs are
  a flat Supabase tier and a hosted app.
