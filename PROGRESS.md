# Progress

Resume point for any future session. **Updated 2026-10-02.** The draft happened on
8 Sep; the project is in **season mode**.

Run `python tools/gate.py` first. The figures below were true when this was written
and are not maintained by anything - the gate's output is.

```
lint         ok      0.4s
types        ok      1.3s
tests        ok     45.5s      659 passed, 1 skipped
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

**Yahoo API access is half-live, and the missing half is on Yahoo's side.**

The app is registered and consent completes, but every data call fails with
`oauth_problem="additional_authorization_required"`. Confirmed 15 Sep by reading the
developer console directly: app `hnkXi0Gh` (Client ID begins `dj0yJmk9bzB0`) shows
only OpenID Connect permissions (Email, Profile). **No Fantasy Sports group exists on
the app at all**, so there is no box to tick. yfpy never sends a `scope` parameter
(`yahoo_oauth/oauth.py:99`). **Asking for it explicitly does not help either -
proven 17 Sep:** `scope=fspt-r` at the authorization endpoint returns
`error=invalid_scope`, while `openid` on the same app is accepted. The remedy is Yahoo
support attaching the scope, then a **fresh consent** signed in as the Yahoo account
that owns league 796511 - the old token cannot gain a scope by refreshing.

Until then:

- **Protocol D is still unsatisfied.** `tests/test_acceptance.py` (reproduce Yahoo's
  displayed weekly points for 10 players) is the single skipped test. The scoring
  engine is verified against hand-computed tests only.
- Waivers/FAAB, playoff odds, and opponent rosters need a live `LeagueSnapshot`
  and degrade with a message rather than using stale data (compliance obligation 1
  deliberately overrides standard 4 for Yahoo).
- Season mode works on **your pasted roster** (`my_roster`, via the dashboard's
  "This week" paste box or the CLI).

Also from that session: loading the app page put the **Client Secret** into a
transcript. Deliberately not rotated yet - Yahoo has no regenerate button, so rotating
means recreating the app, which mints a new Client ID and discards the pending scope
request. Rotate once Fantasy access works.

---

## Phase status

| Phase | State | Evidence |
|---|---|---|
| Storage / schema | **Done** | SQLite + Postgres, numbered migrations; RLS enforced on every apply |
| ID mapping | **Done** | Yahoo IDs memory-only, rebuilt by name each run |
| Scoring engine | **Done, acceptance test blocked** | Hand-computed tests; rules in `src/league_bootstrap.py`, pinned by `tests/test_league_rules.py` |
| Projections + blending | **Done** | Backtested: r 0.67, RMSE 5.63 over 2,205 2025 player-weeks |
| Confidence bands | **Done** | `src/analytics/uncertainty.py`, measured sigma per position |
| VORP / draft board / draft assistant | **Done, used** | Draft night 8 Sep |
| Dashboard | **Done** | Streamlit + Supabase |
| Injuries / byes / lineup / recap | **Done** | Run off the pasted roster |
| Yahoo compliance | **Done** | Yahoo tables dropped; `tools/check_yahoo_persistence.py` in the gate |
| Waivers / FAAB | **Works from a pasted wire** | `fcc waivers --wire-file`, or the paste box on the dashboard's This week tab. Never stored. Rival FAAB profiles still need Yahoo |
| Playoff odds | **Built, blocked on Yahoo scope** | Matchups on the snapshot, not a table |
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

## Open - needs the user

- [ ] **Yahoo support: attach the Fantasy Sports scope** to app `hnkXi0Gh`. Emailed
      twice 15 Sep; a third (the `invalid_scope` evidence, to
      fantasyapiapplications@ and fantasyapideveloper@yahoosports.com) was SENT 19 Sep;
      a fourth, short chaser sent 2 Oct. No reply to any. Still `invalid_scope` on
      2 Oct. When the scope arrives the daily check notifies; then consent in a terminal.
      **2 Oct finding - email is the wrong lever.** The create-app form on this account
      now offers "Fantasy Sports - Read" (seen 2 Oct), i.e. the ACCOUNT is enabled. Per
      yfpy issue #84 (appdesigngeeks 26 Sep, Kemper60 1 Oct), an app created before
      the account was enabled can never gain the scope: create a NEW app with the box
      ticked, submit its Client ID at sports.yahoo.com/developer/application-confirmation/
      (the step the DocuSign completion email of 12 Sep asks for), swap the key and
      secret here and in the GitHub secrets, then a fresh consent.
      **Done 2 Oct:** new app `eenJqhS1` created (Client ID begins `dj0yJmk9Q2NDa`),
      key and secret in `.env`, `fcc yahoo-scope` -> `attached`, consent completed
      (a token exists), confirmation form submitted ~15:40 ET.
      **Still failing 2 Oct 15:42:** every data call returns 403 "This application
      is not authorized to perform this action" - Yahoo has not yet enabled the new
      Client ID. Others waited from zero to seven days with no email. PROBE WITH
      `fcc doctor` (a real call); `yahoo-scope` saying `attached` proves nothing more.
      Yahoo's auto-reply to the form ("received your application ... review typically
      takes 1-2 weeks") arrived 2 Oct - the same text as 8 and 13 Sep, so it carries
      no information. If still 403 on 9 Oct, email fantasyapiapplications@yahoosports.com
      with the new Client ID and the 403 string.
      Unknown: whether the GitHub secret `YAHOO_CLIENT_ID` was updated to the new app.
      Once a call succeeds: Protocol D acceptance test, then delete old app `hnkXi0Gh`
      (which retires the exposed Client Secret).
- [ ] **Was the exposed Supabase data read?** Nine tables were open until 15 Sep.
      Answerable from the PostgREST logs; not yet checked.
- [ ] **Email untested.** `test-notify` sends a real email; last recorded run failed.
- [ ] **Repo is public** with a proprietary LICENSE.
- [ ] Rotate the Yahoo Client Secret, **after** Fantasy access works (see above).
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
- [ ] Protocol D acceptance test, the moment Yahoo returns data.
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
- **QB projections barely predict week to week** (r = 0.32), so the optimiser's QB
  calls are close to noise.
- **TE spread is modelled 21% too narrow and projected 0.87 low**, both pointing the
  same way: a TE's floor is overstated and upside understated.
- `points_actual` still omits **return TDs** (not in nflverse) and **self-recovered
  fumbles** (nflverse publishes only lost ones). Named in `KNOWN_MISSING_STATS`.
- Model constants in `src/analytics/` are fitted on 22,175 player-weeks (2022-25),
  in-sample. The backtest covers one season of one source.
- League settings in `src/league_bootstrap.py` were transcribed by hand from the Yahoo
  settings page, not read from the API. That file is the LIVE scoring configuration
  (`config.yaml` has no scoring section); `tests/test_league_rules.py` pins it.
- **Two different players can share a canonical key.** `make_player_key` is
  name+position, so the two Rodney Smiths (both RB, both free agents) are one row
  and the second overwrites the first's Yahoo and Sleeper ids - which is why sync
  reports 1,604 players carrying a Yahoo id and the table holds 1,603. Predates
  the batching work and survives it unchanged; neither is currently rostered.
