# Progress

Resume point for any future session. **Updated 2026-10-07.** The draft happened on
8 Sep; the project is in **season mode**.

Run `python tools/gate.py` first. The figures below were true when this was written
and are not maintained by anything - the gate's output is.

```
lint         ok      0.4s
types        ok      1.3s
tests        ok     44.2s      691 passed, 1 skipped (the live Yahoo test; FCC_LIVE=1 runs it)
degradation  ok      0.4s
yahoo        ok      0.1s
dead code    ok      0.6s
security     ok      1.5s
```

`python tools/mutate.py` (not in the gate, ~4 min): **10/10 mutations caught, tree
restored**, run 21 Sep. On this machine `python` is not on PATH - use
`.venv/Scripts/python.exe`.

If this file disagrees with `git log`, the log is right. This file went three weeks
and ~40 commits stale once (26 Aug - 16 Sep); update it in the same session as the work.

---

## The one thing to read before anything else

**Yahoo data access is LIVE as of 7 Oct 2026, on a NEW app.** `fcc doctor` says
`working`; `fcc verify-scoring` passes.

What it took, so nobody repeats the detour: the original app `hnkXi0Gh` could never
gain the Fantasy Sports scope - it was created before Yahoo enabled the account, and
Yahoo's own answer to that (via yfpy issue #84) is "create a new app". App `eenJqhS1`
(Client ID begins `dj0yJmk9Q2NDa`) was created 2 Oct with "Fantasy Sports - Read"
ticked, its Client ID submitted at `sports.yahoo.com/developer/application-confirmation/`,
consent completed the same day, and data calls were refused with 403 until Yahoo
enabled the Client ID five days later (the "access is live" email arrived 7 Oct and,
this time, was true). Four emails to Yahoo over three weeks achieved nothing; the
form did.

Where the scheduled runs stand: **GitHub has the full Yahoo credential set since 7 Oct
13:12 UTC** (`YAHOO_CONSUMER_KEY`, `YAHOO_CONSUMER_SECRET`, `YAHOO_ACCESS_TOKEN_JSON`,
set by `tools/push_yahoo_secrets.py`; `YAHOO_CLIENT_ID` for the scope check). A
dispatched `doctor` run reported `yahoo oauth : working` and a `sync` run pulled 12
teams, 191 slots, 199 free agents, 92 transactions on the runner. The pasted roster
(`data/roster.txt`) is now only the fallback for a refused call. If a run warns that
Yahoo rotated the refresh token: `fcc verify-settings` in a terminal, then re-run
`tools/push_yahoo_secrets.py`.

**Delete the old app `hnkXi0Gh`** in the Yahoo developer console. Its Client Secret
was in a transcript on 15 Sep; deleting the app retires it. Nothing here references it.

---

## Phase status

| Phase | State | Evidence |
|---|---|---|
| Storage / schema | **Done** | SQLite + Postgres, numbered migrations; RLS enforced on every apply |
| ID mapping | **Done** | Yahoo IDs memory-only, rebuilt by name each run |
| Scoring engine | **Done, Protocol D PASSED 7 Oct** | `fcc verify-scoring`: 61 roster player-weeks (wk 1-4) vs Yahoo's listed points, 53 exact, 0 differ, 8 inactive, defenses included. Rules in `src/league_bootstrap.py`; `verify-settings` finds no value difference |
| Projections + blending | **Done** | Backtested 7 Oct on the box-score ground truth: r 0.65, RMSE 5.62 over 2,473 2025 player-weeks (defenses now included) |
| Confidence bands | **Done** | `src/analytics/uncertainty.py`, sigma per position incl. DEF (5.44), refit 7 Oct |
| VORP / draft board / draft assistant | **Done, used** | Draft night 8 Sep |
| Dashboard | **Done** | Streamlit + Supabase |
| Injuries / byes / lineup / recap | **Done** | Live Yahoo roster locally; pasted roster on GitHub. A refused Yahoo call falls back to the pasted roster, loudly |
| Yahoo compliance | **Done** | Yahoo tables dropped; `tools/check_yahoo_persistence.py` in the gate |
| Waivers / FAAB | **Live** | `fcc sync-league`: 12 teams, 190 slots, 199 free agents, 81 transactions (7 Oct). Pasted wire still works without Yahoo. Never stored |
| Playoff odds | **Live** | First run 7 Oct: 91.7% playoffs, 8.5% title, seed 3.5, from live standings and schedule |
| Trades | **Partial** | `fcc offer` evaluates any N-for-M offer against your starting lineup; `trades` job still proposes 1-for-1 only |

## Done since the last update (26 Aug - 17 Sep)

- **Compliance with the signed Yahoo agreement** - nothing Yahoo-derived persisted,
  `fcc purge-yahoo`, attribution, a gate that fails on writes to Yahoo tables.
- **First non-circular measurement** (`tools/backtest.py`) - projections before a week
  vs points scored in it. Replaces the circular "beats ADP" benchmark.
- **Row-level security** (`2610524`) - Supabase flagged 9 tables world-readable and
  writable. `enforce_rls()` now secures every public table on each schema apply.
  Verified against the live project: 0 of 22 exposed.
- **`points_actual` was missing fumbles and two-point conversions** (`7adc71c`) -
  4.0% of player-weeks wrong by up to 4.0 points. Fixed, re-synced (Goff 2025 wk17:
  10.08 -> 6.08), sigma refit (QB 6.94 -> 7.07, pooled 5.60 -> 5.62). A coverage test
  now fails the build if a scored category stops reaching the ground truth.
- **`doctor` tells the truth** - calls Yahoo for real (`9467f3d`), names a token that
  predates its scope (`d19a8f7`), and no longer hangs on a verifier prompt when there
  is no token (`8d5e2bd`).
- **Daily scope check** (`f3d304b`) - `fcc yahoo-scope` asks Yahoo each morning
  (after the injury run on GitHub, Client ID in its own `YAHOO_CLIENT_ID` secret)
  and notifies the day Fantasy Sports is attached. `doctor` says the same thing.
- **Sunday lineup cron** (`311ad60`) - the job picker no longer matched the moved
  Sunday cron; fixed before its first Sunday, with a test on every cron.
- **Jobs no longer start a Yahoo sign-in** (`d01f175`) - with a Client ID and no
  token every season job and `fcc sync` crashed on "Enter verifier". Consent is
  `fcc verify-settings` in a terminal; jobs need a token.
- **Waivers from a pasted wire** (`7969f34`, dashboard in the next commit) - plus
  three report fixes: free agents no longer get FAAB bids, no invented "0%
  rostered", handcuffs only count if on the wire.
- **The morning run was dead for two days** (18-19 Sep) - `fcc sync` overran the
  20-minute job timeout, so the injury monitor and everything after it never ran,
  and the orphan's locks took the scope check down with it. Timeout raised to 45
  (`e5e3d49`), the scope check now answers without a database (`5e0c1eb`), and
  sync says where its time goes (`775084b`).
- **sync: 925s -> 37s**, measured against the live database on 19 Sep. It wrote a
  row per statement over a 20ms link; the hot loops now batch through
  `Database.executemany`, which psycopg 3 pipelines. players 392.7s -> 2.5s,
  season proj 207.4s -> 1.1s, adp 88.9s -> 0.5s, blending 78.8s -> 0.9s. Same
  counts, same rows, ids still merged (`e8e5d81`, `23dcffc`, `2ae2e8f`, `a737ca3`).
  `sync-usage` is 1m26s.
- Recap no longer blames you for swaps you could not have made (`d88e9ae`).

## Done 20 Sep - the METHOD audit (`docs/METHOD-audit.md`)

Found by breaking the code on purpose, not by reading it.

- **A check that read nothing reported success** (`07efa65`) - both gate checkers
  exited 0 over an empty file set. Absent evidence is now NOT PROVEN and fails.
- **Deleting RLS enforcement left every RLS test green** (`af32ba4`) - the function
  was tested, the wiring in `schema.apply` was not. Both call sites now asserted.
- **The league's scoring rules were pinned by nothing** (`16bee1e`) - zeroing the
  interception override left scoring and golden suites green.
  `tests/test_league_rules.py` now asserts them through the real settings.
- **`tools/mutate.py`** (`5bb98d6`) - ten deliberate defects, each must turn
  something red. Refuses a non-unique target; restores byte for byte.
- **`CLAUDE.md` gained pause triggers** (`ff14ced`) and now says the truth about
  Yahoo: agreement countersigned 13 Sep, scope never attached.
- **`fcc catchup`** (`30a8f99`) - the Sunday 20 Sep lineup run arrived 3h05m late,
  35 minutes after kickoff. GitHub cron is late every time (2-4h typical, 5h30m
  worst over twenty runs). Every run now ends by running what was due and has
  not run; a job that still cannot run becomes a notification. `src/schedule.py`
  is the only copy of the schedule; the workflow asks `fcc which-job`.

## Done 2-7 Oct - Yahoo goes live

- **New Yahoo app** (above). Scope check, consent, confirmation form, five-day wait.
- **A refused Yahoo call no longer kills a job** (`38ec72c`) - with a token but no
  data access every job died on a 403 traceback; now it falls back to the pasted
  roster and says so. `sync` reports the refusal and finishes.
- **Announcements go out once, ever** (`0ae6044`) - the "scope attached" email was
  sent from the laptop and again from GitHub; `Notifier.ever_sent` matches on title.
  With a token the daily check makes a real call and announces the first success.
- **Protocol D run for real** (`ce7073d`, `b29e490`, `03d90e8`) - 32/45 exact on
  the first pass; all four misses were fumbles. `ff_opportunity` has no sack fumbles
  and no self-recovered ones. The ground truth now comes from nflverse's weekly
  `player_stats` (box score), which also carries the return TDs:
  `KNOWN_MISSING_STATS` is empty. Defenses got actuals from team stats + the game
  score (JAX 13, LAC 4, DET 7, DET 2 - all exact). `fcc verify-scoring` is the
  re-runnable check; `tests/test_verify_scoring.py` runs it under `FCC_LIVE=1`.
- **First live `sync-league`** (`71b4d28`) - two guessed shapes: `?format=json`
  sent twice (400), `data_type_class=Player` mis-parsing the list; and all 16
  unmatched roster slots were defenses (Yahoo says "Eagles"/"Phi", we key DEF|PHI).
- **Sigma refit on the corrected truth** (`5beeea9`) - QB 7.24, RB 5.86, WR 5.48,
  TE 4.50, DEF 5.44 (first measurement), pooled 5.61.
- **Pasted roster for week 4** loaded from the Yahoo app screenshot; all 16 matched.
- **GitHub runs on the live league** (`f9c4522` + secrets 7 Oct 13:12 UTC). Setting
  the secrets needed a Claude Code permission rule for `gh secret set`, added to
  `.claude/settings.local.json` (gitignored) at the user's explicit request.

## 7 Oct - the deep scrub (user: "make this world class"; plan of 7 phases)

Phase 1 (`4f4c673`): dashboard week from the NFL state (it defaulted to 1);
Yahoo's injury tag for the user's roster; PAYOUTS and the calendar recorded.
Phase 2 (`d166c0d`, `b2feaf5`, `6edb982`, this commit) - data audit:
- weekly projections were SLEEPER ONLY. Now Sleeper + ESPN (same payload,
  asked for the week) + FantasyPros weekly expert consensus via nflverse's
  mirror (`src/sources/weekly_consensus.py`, calibrated per position from
  full-PPR onto this league's scale; WR factor 0.78). Week 5: 249 players
  blended from three sources, 112 from two, 122 from one (mostly DEF: Sleeper
  + consensus only, ESPN's D/ST not parsed).
- injury tags: 125 of 390 disagreed with Yahoo (stale Sleeper 'Questionable').
  Yahoo's tag now rides on the snapshot for every player it exposes and
  overrides the feed in lineup and waivers. Byes and teams: clean.
- `verify-scoring --notify` runs after Monday's recap; mails only on a miss.
- FantasyPros' own projection pages render 10 rows without JS/login: not a
  source. CBS pages are server-rendered (102 rows) - a possible fourth.
Open from the audit: ESPN D/ST weekly; `game_context` empty until ODDS_API_KEY
(user signing up; the workflow passes it through); the hosted dashboard still
needs its three Yahoo secrets pasted at the PC.
Decisions recorded: 14 regular-season weeks, 13 of them paying the $50 high
score (which week is unpaid: ask the commissioner); Odds API free tier: yes.

## Open - needs the user

- [x] **Scheduled runs use Yahoo** (7 Oct).
- [ ] **Hosted dashboard secrets.** The code is live-ready (`_live_league` in
      dashboard.py: live snapshot, 10-min in-memory cache, paste as fallback, waivers
      from the live wire). Streamlit Community Cloud has no CLI for secrets, so at the
      PC: `tools/push_yahoo_secrets.py --streamlit`, paste the three lines at
      share.streamlit.io -> app -> Settings -> Secrets, reboot the app, clear the
      terminal. Until then the hosted app says "Showing your pasted roster".
- [ ] **Delete old app `hnkXi0Gh`** in the Yahoo console (retires the leaked secret).
- [ ] **Unresolved Yahoo name:** "Bam Knight" (Zonovan Knight, RB) - nickname vs legal
      name; 1 of 199 free agents. Add an alias or leave.
- [ ] **Was the exposed Supabase data read?** Nine tables were open until 15 Sep.
      Answerable from the PostgREST logs; not yet checked.
- [ ] **Email untested.** `test-notify` sends a real email; last recorded run failed.
- [ ] **Repo is public** with a proprietary LICENSE.
- [ ] **Move the lineup crons ~3 hours earlier?** They assume a punctuality GitHub
      never delivers. Must keep the Thursday-to-Sunday gap above the 72-hour dedup
      window. A decision, not a fix (`docs/METHOD-audit.md` open item 1).

## Open - code

- [ ] **Playoff odds from a paste** - approved 17 Sep with waivers. Needs a real paste
      of the standings page and of the league schedule before the parser is designed.

- [ ] **The review-and-guardrails pass was cut short on 16 Sep** after its first
      finding (`points_actual`). The rest of the codebase has not had that pass.
- [ ] `trades` job: 2-for-1 proposals, and wiring to the buy-low / sell-high signal
      in `src/analytics/regression.py` (not referenced by `src/season/trades.py`).
- [ ] From the METHOD audit, in its priority order: **no backup** of Supabase;
      **no post-deploy check** against the real dashboard URL; the **weekly flow has
      never been walked end to end**; this file's numbers are hand-maintained (a
      `--counts` flag would derive them).
- [ ] `mypy tests` reports **five** errors in three files (21 Sep): `test_gates.py:119`
      `_write` redefined, `test_rls.py:140` uses the value of `list.append`, and three
      unused `type: ignore` in `test_yahoo_scope.py` (60, 225, 274). The gate type-checks `src`, `dashboard.py` and `fcc.py` only.

## Known-weak claims

Written down so no future session repeats them as fact:

- "Beats naive ADP by 118 points" and "best roster in 36/36 drafts" are **circular**
  (both scored opponents with our own projections). The backtest is the real number.
- **QB and DEF projections barely predict week to week** (r = 0.31 each), so the
  optimiser's calls at those slots are close to noise.
- **TE spread is modelled 21% too narrow and projected 0.80 low**, both pointing the
  same way: a TE's floor is overstated and upside understated.
- `points_actual` is now the nflverse box score and matched Yahoo on every one of
  61 roster player-weeks checked (7 Oct). It has been checked for ONE roster over
  four weeks; `fcc verify-scoring` each week is what keeps that claim honest.
- Model constants in `src/analytics/` are fitted on 22,175 player-weeks (2022-25),
  in-sample. The backtest covers one season of one source.
- League settings in `src/league_bootstrap.py` were transcribed by hand from the Yahoo
  settings page, not read from the API. That file is the LIVE scoring configuration
  (`config.yaml` has no scoring section); `tests/test_league_rules.py` pins it.
- **The FAAB dollars-per-point model has almost nothing to learn from.** 45 of 45
  winning bids now resolve to players (7 Oct), but only 1 carries a value: a waiver
  pickup is by definition near replacement level, so "ROS points above replacement"
  is 0 for nearly all of them and beta stays on the $1.20/pt prior. What saves the
  recommendation is `position_market`: the position's own observed bids (12 DEF
  claims for $0-$10, median $0) cap the price. The prior said $65 for a defense.
- **Two different players can share a canonical key.** `make_player_key` is
  name+position, so the two Rodney Smiths (both RB, both free agents) are one row
  and the second overwrites the first's Yahoo and Sleeper ids - which is why sync
  reports 1,604 players carrying a Yahoo id and the table holds 1,603. Predates
  the batching work and survives it unchanged; neither is currently rostered.
