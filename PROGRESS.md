# Progress

Resume point for any future session. **Updated 2026-09-17.** The draft happened on
8 Sep; the project is in **season mode**.

Run `python tools/gate.py` first. The figures below were true when this was written
and are not maintained by anything - the gate's output is.

```
lint         ok      0.1s
types        ok      0.7s
tests        ok     37.8s      594 passed, 1 skipped
degradation  ok      0.3s
yahoo        ok      0.1s
dead code    ok      0.5s
security     ok      1.4s
```

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
| Scoring engine | **Done, acceptance test blocked** | Hand-computed tests; rules in `config.yaml` |
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
- Recap no longer blames you for swaps you could not have made (`d88e9ae`).

## Open - needs the user

- [ ] **Yahoo support: attach the Fantasy Sports scope** to app `hnkXi0Gh`. Emailed
      twice 15 Sep, no reply. A third email (the `invalid_scope` evidence, cc
      fantasyapiapplications@yahoosports.com) is DRAFTED in Gmail, not sent. When the
      scope arrives the daily check notifies; then consent in a terminal.
- [ ] **Was the exposed Supabase data read?** Nine tables were open until 15 Sep.
      Answerable from the PostgREST logs; not yet checked.
- [ ] **Email untested.** `test-notify` sends a real email; last recorded run failed.
- [ ] **Repo is public** with a proprietary LICENSE.
- [ ] Rotate the Yahoo Client Secret, **after** Fantasy access works (see above).

## Open - code

- [ ] **Playoff odds from a paste** - approved 17 Sep with waivers. Needs a real paste
      of the standings page and of the league schedule before the parser is designed.

- [ ] **The review-and-guardrails pass was cut short on 16 Sep** after its first
      finding (`points_actual`). The rest of the codebase has not had that pass.
- [ ] `trades` job: 2-for-1 proposals, and wiring to the buy-low / sell-high signal
      in `src/analytics/regression.py` (not referenced by `src/season/trades.py`).
- [ ] Protocol D acceptance test, the moment Yahoo returns data.
- [ ] `mypy tests` reports one error (`tests/test_gates.py:119`, `_write` redefined).
      The gate type-checks `src`, `dashboard.py` and `fcc.py` only.

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
- League settings in `config.yaml` were transcribed by hand from the Yahoo settings
  page, not read from the API.
