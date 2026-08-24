# Fantasy Command Center

A season-long advantage engine for the Yahoo league **Extra Fun League** (ID `796511`),
team **Butt Fumblers**.

Two modes:

- **Draft Mode** — a live draft assistant that ranks every available player by value
  over replacement, positional scarcity, your roster needs, and the probability each
  player survives until your next pick.
- **Season Mode** — scheduled jobs that watch injuries, waivers, byes, lineups and
  matchups, and surface prioritized actions before your league-mates react.

Everything is driven by your league's **actual** Yahoo settings. Nothing about the
scoring or roster rules is hardcoded.

---

## Your league, as configured

Read from the Yahoo league settings page on 2026-08-23:

| Setting | Value |
| --- | --- |
| Teams | 12 |
| Starters | QB, WR, WR, RB, RB, TE, **W/R/T, W/R/T**, DEF |
| Bench / IR | 5 / 2 |
| Kicker | **none** — kickers are excluded from the board entirely |
| Scoring | Half PPR (0.5/rec), 4pt pass TD, **−1 INT**, 6pt rush/rec TD |
| Fumbles | **−1 fumble AND −1 fumble lost** (they stack) |
| Waivers | FAAB, continual rolling tiebreak, process Tuesday game time |
| Season acquisitions | 75 max |
| Playoffs | 6 teams, weeks 15–17 |
| Draft | **Tue Sep 8, 8:30pm EDT** — live standard snake |

Two flex spots is the detail that most changes the maths: it pushes replacement
level down to **RB33 / WR39** (a 1-flex league would be RB28/WR32), which raises
the value of every startable running back and receiver.

---

## Quick start

```bash
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

python fcc.py sync          # pull players, projections, ADP, injuries, byes
python fcc.py rank          # print the draft board
python fcc.py dashboard     # open the Streamlit draft board
```

`sync` needs no credentials. Yahoo auth is only required for live draft polling
and for the season jobs that read your roster.

### Yahoo API setup (needed for live draft sync + season jobs)

```bash
python fcc.py setup           # prompts for your Yahoo app credentials
python fcc.py sync-settings   # opens a browser once for consent
```

Yahoo changed this flow, so the steps below reflect the form as it actually is
(checked 2026-08-24), not the older guides:

1. Create an app at <https://developer.yahoo.com/apps/create/>
   - **Application Name**: `Fantasy Command Center`
   - **Description**: must NOT contain the word "Yahoo" — the form rejects it
   - **Homepage URL** and **Redirect URI**: `https://localhost:8080`
     (the form now requires `https`; the old `oob` value is refused)
   - **OAuth Client Type**: Confidential Client
   - **API Permissions**: leave everything unchecked. There is no longer a
     Fantasy Sports option here — see below.
2. Click **Create App** and copy the Client ID and Client Secret.
3. **Apply for Fantasy Sports API access** at
   <https://sports.yahoo.com/developer/access/>. Yahoo now gates the Fantasy
   Sports API behind a review. The form asks for the product, the data you need,
   your user base, and expected users; supply your Client ID from step 2. State
   plainly that it is personal, single-league, read-only use. Yahoo warns that
   "incomplete or insufficiently detailed submissions cannot be evaluated and
   will be closed without further correspondence", and publishes no approval
   timeline.
4. Once approved, run:

```bash
python fcc.py setup           # prompts for the Client ID and Secret
python fcc.py sync-settings   # opens a browser once for consent
```

Credentials are written to `.env`, which is gitignored. `setup` prompts for them
locally so they are never pasted into a chat transcript. The token refreshes
itself after the first consent.

If consent fails with a `redirect_uri` mismatch, the underlying `yahoo_oauth`
library defaults to the legacy out-of-band value (`CALLBACK_URI = 'oob'`) while
your app is registered with `https://localhost:8080`. It accepts a `callback_uri`
override, so set that to match the registered URI.

**None of this is required to draft.** The board, recommendations, VORP,
survival model and dashboard all run without any Yahoo credentials. Approval only
unlocks live pick syncing during the draft and the season jobs that read your
roster.

`sync-settings` replaces the captured bootstrap settings with the live API
response and auto-detects your team id. Run `python fcc.py verify-settings` to
diff the two — it should report an exact match.

---

## Draft day

```bash
python fcc.py dashboard
```

Three panes: recommendations, the full sortable board with tier colouring, and
your roster with remaining needs. There is a search box, big tap targets, and an
undo.

**Manual mode is the default and always works.** Toggle "Poll Yahoo for picks" in
the sidebar to sync live; if that fails mid-draft the banner turns red and you
keep going by tapping players as they are taken. Nothing else changes.

Rehearse first:

```bash
python fcc.py mockdraft --slot 5              # one draft from slot 5
python fcc.py mockdraft --compare -n 100      # the strategy benchmark
```

### How a recommendation is built

```
score = VORP × need_multiplier × tier_urgency
```

- **VORP** — projected points minus the last startable player at that position.
  This is why Josh Allen sits around board rank 18 despite the highest raw
  projection in the league: replacement-level QBs also score ~297.
- **need_multiplier** — weights positions by the starting slots you have not
  filled, shares flex need across eligible positions, and suppresses DEF until
  round 12. Hard caps stop it drafting a second defense, which cannot start.
- **tier_urgency** — boosts the last player in a tier when the survival model
  says he will not return to you.

Position caps and a surplus discount stop the late rounds stacking players who
can never start. VORP measures a player against replacement *as a starter*, so a
third tight end still grades above TE12 despite being unstartable and freely
streamable; `draft.te_flex_credit` controls whether TE is assumed to claim a flex
spot (default 0 — the flex goes to a RB/WR almost every week).

Survival is `P(available at pick N) = 1 − Φ((N − ADP) / σ)`, with σ taken from
FantasyPros expert disagreement where available and a fitted curve otherwise
(pick 3 is predictable; pick 103 is not).

---

## Season jobs

| Job | When | What it does |
| --- | --- | --- |
| `waivers` | Tue 07:00 | ROS value vs. your worst droppable, explicit ADD/DROP pairs, FAAB bid ranges, stashes, unrostered handcuffs |
| `injuries` | daily 08:00 | Diffs status changes against yesterday for your roster, your opponent, and top free agents |
| `lineup` | Thu 10:00, Sun 09:00 | Exact optimal lineup vs. what you have set; alerts only on differences |
| `byes` | Wed 07:00 | Next 4 weeks of bye/injury gaps, plus playoff-week schedules |
| `recap` | Mon 08:00 | Score, optimal score, and points left on your bench |
| `trades` | Mon 08:15 | Mutually beneficial trade ideas with the value maths shown |

Run any of them directly. `--dry-run` prints the notification instead of sending it:

```bash
python fcc.py injuries --dry-run
python fcc.py waivers --budget 73
python fcc.py daily            # sync, then run whatever is due today
```

### Scheduling

```powershell
powershell -ExecutionPolicy Bypass -File jobs\install_schedule.ps1 -WhatIf   # preview
powershell -ExecutionPolicy Bypass -File jobs\install_schedule.ps1           # install
powershell -ExecutionPolicy Bypass -File jobs\install_schedule.ps1 -Remove   # uninstall
```

Tasks are registered with `StartWhenAvailable`, so a run missed while the machine
was asleep happens at the next opportunity. Every job is idempotent, so
`python fcc.py daily` by hand is always a valid substitute.

`jobs/install_schedule.sh` does the same via cron on macOS/Linux.

### Notifications

Configure in `config.yaml`. Discord is the easiest path to your phone: create a
webhook and put the URL in `.env` as `DISCORD_WEBHOOK_URL`, then set
`notifications.discord.enabled: true`. Email (SMTP) and desktop toasts are also
supported. Each channel is skipped silently when unconfigured.

Notifications are deduplicated on the *fact*, not the delivery — an unchanged
Questionable tag will not be re-sent every morning.

**The system never executes transactions.** Every recommendation tells you the
exact action to take in Yahoo.

---

## Data sources

Every fetch is cached in SQLite with a timestamp. When a source is down, jobs fall
back to the last good copy and warn; they never crash.

| Source | Provides | Auth |
| --- | --- | --- |
| Yahoo (`yfpy` 17) | League settings, scoring, rosters, draft results, transactions, free agents | OAuth |
| Sleeper | Player database, injuries, trending adds/drops, **projections + ADP** | none |
| ESPN | Second projection opinion (top ~500 by ownership) | none |
| FantasyPros (via ffverse) | Expert consensus rank, expert disagreement, bye weeks | none |
| nflverse (`nflreadpy`) | Schedule, bye weeks, playoff SOS, historical variance | none |

Notes on what was verified rather than assumed:

- **`nfl_data_py` is not usable** — it pins `pandas<2` and `numpy<2` and will not
  install on current Python. `nflreadpy` is the maintained nflverse successor.
- **Sleeper projections live at the API root**, not under the documented `/v1`
  prefix: `api.sleeper.app/projections/nfl/{season}[/{week}]`.
- **Sleeper carries `yahoo_id`** for ~1,600 players, which makes Yahoo↔Sleeper
  identity an exact join rather than a fuzzy name match.
- **Sleeper does not expose practice participation** despite the field being
  widely assumed to exist; it is absent from every record in the live dump.
  Injury status and body part are available.
- **Sleeper's DEF season line is degenerate** (`gp: 1.0`, a lone `pts_allow_0`),
  so defenses are rebuilt by summing the well-formed weekly lines. Points allowed
  is the largest component of DST scoring and would otherwise contribute zero.
- **`def_td` is already the total** of its components; summing them double-counts.

### Scoring engine

Every projection source's **raw stat line** is converted to points using your
league's own modifiers. No source's pre-computed points are ever used — which
matters here, because this league overrides two Yahoo defaults (INT −1 instead of
−2, and fumbles −1 instead of 0).

---

## Running it anywhere (hosted)

The app runs in two places from the same codebase. Which one is decided by a
single environment variable:

| | Local | Hosted |
| --- | --- | --- |
| Database | `data/league.db` (SQLite) | Supabase Postgres |
| Chosen by | no `DATABASE_URL` set | `DATABASE_URL` set |
| Scheduler | Task Scheduler / cron | GitHub Actions |
| Needs your PC awake | yes | no |

### 1. Database

The Supabase project `fantasy-command-center` already has the schema applied,
with row-level security enabled and no policies - so the publishable anon key
can read nothing, and only a direct connection as the owner reaches the data.

**The database password is not recoverable.** Supabase generates one at
project creation and never shows it again, so the first step is to set one you
know: **Database -> Settings -> Reset database password**, then copy it.

Then take the connection string from **Connect** (top bar of the project) ->
**Direct** -> Connection method **Session pooler** -> Type **URI**.

Avoid "Direct connection": it resolves over IPv6, which neither Streamlit
Community Cloud nor GitHub Actions can reach without the paid IPv4 add-on. The
session pooler is the IPv4 path. The transaction pooler also works - the client
disables prepared-statement promotion, which is what usually breaks under
transaction pooling.

Put it in `.env` locally:

```
DATABASE_URL=postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres
```

Then move the local data across (safe to re-run; it tops up rather than
duplicating):

```bash
python fcc.py migrate --dry-run    # see what would move
python fcc.py migrate
python fcc.py doctor               # confirms which backend it is talking to
```

### 2. Dashboard on Streamlit Community Cloud

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. **New app** -> repo `sachin7421/fantasy-command-center`, branch `main`,
   main file `dashboard.py`.
3. Under **Advanced settings -> Secrets**, paste the contents of
   `.streamlit/secrets.toml.example` with your real values - at minimum
   `DATABASE_URL` and `APP_PASSWORD`.
4. Deploy. The URL works from any device, and the password gate protects it.

`APP_PASSWORD` is what keeps a public URL private. With no password configured
the gate stays open, which is what you want locally but not on the internet.

### 3. Scheduled jobs on GitHub Actions

`.github/workflows/jobs.yml` runs the season jobs on schedule. Add the same
secrets under **repo Settings -> Secrets and variables -> Actions**:

- `DATABASE_URL` (required)
- `DISCORD_WEBHOOK_URL` (optional, for phone alerts)
- `YAHOO_CONSUMER_KEY`, `YAHOO_CONSUMER_SECRET`, `YAHOO_ACCESS_TOKEN_JSON`
  (optional, once Yahoo API access is approved)

Run one by hand from the **Actions** tab -> *Scheduled jobs* -> *Run workflow*.

Cron in GitHub Actions is UTC-only, so the schedules are written for Eastern
daylight time. After DST ends each job lands an hour later in local time, which
is harmless: every job is idempotent and the Tuesday waiver run still finishes
long before waivers process.

### Data retention

The hosted database is also the long-term record. Alongside the current-state
tables, these are append-only and never overwritten:

| Table | What accumulates |
| --- | --- |
| `projection_history` | every projection ever observed, per source and week |
| `player_week_actuals` | what players actually scored under this league's rules |
| `adp` | ADP / expert-consensus movement over time |
| `injuries` | the full injury timeline, not just the current status |
| `trending` | waiver-wire heat, sampled continuously |
| `recommendations` | every recommendation the system made |
| `matchups`, `standings_history` | weekly results and standings |

Together those answer questions that need a season of data: which projection
source was actually most accurate for this league's scoring, whether trending
adds predicted anything, and whether the tool's own advice was any good.

---

## Project layout

```
config.yaml              league id, schedule, notifications, risk preferences
.env                     Yahoo credentials + token (gitignored)
fcc.py                   CLI entry point
dashboard.py             Streamlit draft board + season view
src/
  scoring.py             stat lines -> league points
  projections.py         blending + floor/ceiling
  vorp.py                replacement levels, VORP, tiers, scarcity
  lineup_solver.py       exact optimal-lineup assignment
  idmap.py               cross-source player identity
  yahoo_client.py        yfpy wrapper
  league_bootstrap.py    verified settings, used until OAuth is configured
  draft/                 survival model, recommender, live tracker, mock drafts
  season/                waivers, injuries, lineup, byes, recap, trades
  sources/               sleeper, sleeper_projections, espn, fantasypros, nflverse
jobs/install_schedule.*  Task Scheduler / cron installers
tests/                   107 tests
```

---

## Tests

```bash
.venv\Scripts\python -m pytest tests -q
```

Covering the acceptance criteria:

- **Scoring** — reproduces hand-computed totals for QB/RB/WR/K/DST lines, proves
  reception value comes from league settings rather than a constant, and checks
  the points-allowed brackets tile the range with no gaps or overlaps.
- **Lineup solver** — includes the case where greedy flex-filling strands a
  position-locked player, which is why the solver is an exact assignment.
- **Identity** — punctuation, suffixes, nicknames, team changes, and a guarantee
  that an unknown name is rejected rather than confidently mismatched.
- **Draft** — snake order, survival monotonicity, tier breaks, simulated flex
  allocation, and the position caps.
- **Blending** — weight renormalization, and a regression test for the bug where
  a source missing from the config was silently dropped to zero weight.

### Draft strategy benchmark

`python fcc.py mockdraft --compare -n 100` runs 100 paired drafts: the same seed
is played twice, once with the recommender in your seat and once with a naive ADP
drafter, so opponents behave identically and the difference is attributable to
strategy. The ADP baseline is a genuinely strong opponent — it encodes the whole
market's opinion — and is given the same roster discipline (no stacking four
defenses) so the comparison is fair.

Result on the current board: **+163 projected starting-lineup points**, a 100% win
rate, and a mean finish of 1.0 of 12 versus 6.3 for the baseline.

Treat that as directional rather than a promise: the benchmark scores teams with
*our own* projections while opponents follow ADP, so it partly measures how much
our projections disagree with the market. The edge is real only to the extent the
underlying projections are.

---

## Known gaps

- **Yahoo projections are not blended in.** Yahoo does not expose player
  projections through the public Fantasy API; the `yahoo` weight in `config.yaml`
  is wired but inert unless that changes.
- **Playoff SOS is schedule-only.** It lists weeks 15–17 opponents but does not
  yet rate matchup difficulty against defensive strength.
- **`recap` falls back to projections** when actual weekly results have not been
  synced; the numbers are labelled but are not real scores until Yahoo sync runs.
- **ESPN does not contribute defenses** — its DST points-allowed is shaped as
  per-game bucket counts rather than a scalar, so mixing it in would add error.
