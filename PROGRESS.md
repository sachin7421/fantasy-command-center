# Progress

Resume point for any future session. Updated 2026-08-26. Draft is **Tue 8 Sep 2026,
8:30pm EDT — 13 days away.**

Run `python tools/gate.py` first. If it is green, the state below is accurate.

```
lint         ok      0.1s
types        ok      1.1s
tests        ok     16.5s      388 tests: 387 passed, 1 skipped
degradation  ok      0.2s
dead code    ok      0.5s
security     ok      1.4s
```

---

## The one thing to read before anything else

**Protocol D is not satisfied, and everything downstream of it is already built.**

The acceptance test that D names — reproducing Yahoo's *displayed* weekly points for
10 real players — is the single skipped test in the suite:

```
SKIPPED tests/test_acceptance.py:79: Needs live Yahoo access to read back
Yahoo's own computed weekly points.
```

It cannot run without Yahoo API access, which is not approved yet. The scoring engine
is instead verified against **32 hand-computed tests** in `tests/test_scoring.py`,
covering this league's two overrides (interceptions −1, fumbles −1 *and* fumbles lost
−1), half-PPR receptions read from settings rather than hardcoded, points-allowed
bucket exclusivity, and kicker distance bands.

That is a genuinely weaker guarantee than D asks for: hand-computed tests prove the
engine matches *my* reading of the rules, not that it matches *Yahoo's arithmetic*.
The gap closes the moment Yahoo access lands, and closing it is the highest-value
thing that access unlocks.

---

## Phase status

| Phase | State | Evidence |
|---|---|---|
| Storage / schema | **Done** | Dual SQLite+Postgres, versioned migrations, 41 tests |
| ID mapping | **Done** | 23 tests |
| Scoring engine | **Done, acceptance test blocked** | 32 tests hand-computed; see above |
| Projections + blending | **Done** | 16 tests, 5 sources healthy |
| VORP / draft board | **Done** | 3,134 players on the live board |
| Draft assistant | **Done** | 36 simulated drafts, 6,480 picks, 0 failures |
| Dashboard | **Done** | Live on Streamlit + Supabase |
| Injuries | **Done** | Ran clean 2026-08-26 |
| Byes / lineup / recap | **Done** | Tested; not exercised in-season |
| Waivers / FAAB | **Blocked — Yahoo** | `rosters`, `free_agents`, `transactions`, `team_budgets` all 0 rows |
| Playoff odds | **Not built** | `matchups`, `standings_history` have no writer — this is missing code, not missing Yahoo |
| Trade scout | **Partial** | 1-for-1 only; not wired to the sell-high signal in `regression.py` |

## Live data (2026-08-26)

```
players               3,291      rosters            0   <- Yahoo
projections           7,430      free_agents        0   <- Yahoo
projections_blended   3,830      transactions       0   <- Yahoo
adp                   9,875      team_budgets       0   <- Yahoo
injuries              1,610      draft_picks        0
```

## Open — needs the user

- [ ] **Manual UI rehearsal.** 15 rounds in the real dashboard under time pressure.
      The simulator drives the logic, not the Streamlit layer, and that layer is where
      the worst bug so far lived (`disabled=not is_mine` locked the user out of his own
      pick). **Highest remaining risk to draft night.**
- [ ] **Email untested.** `test-notify` has one recorded failure, not re-run because it
      sends a real email.
- [ ] **Repo is public** with a proprietary LICENSE.
- [ ] Yahoo API approval (external).

## Open — code

- [ ] `matchups` / `standings_history` sync → unblocks playoff odds
- [ ] Trade scout: 2-for-1, and wire to the buy-low/sell-high signal
- [ ] **Replace the circular benchmark.** Both benchmarks score us *and* our opponents
      with our own projections, which measures self-consistency, not edge. A backtest
      against realized points is the only thing that settles it.
- [ ] `draft_results` table has no writer — believed dead, unverified

## Known-weak claims

Written down so no future session repeats them as fact:

- "Beats naive ADP by 118 points" and "best roster in 36/36 drafts" are **circular**.
- Model constants in `src/analytics/` are calibrated on 22,175 player-weeks (2022–25)
  via `tools/calibrate.py` — real, but in-sample.
- `src/league_bootstrap.py` settings were transcribed by hand from the Yahoo settings
  page, not read from the API.
