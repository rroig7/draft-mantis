# Draft Mantis

NBA fantasy draft analysis for the 2026-27 season. It pulls projections, ADP, injury news and history from public sources, then gives every player a **Mantis Score** (0-100) and a draft-room board you can use live during your draft.

No dependencies beyond Python 3.10+ (stdlib only).

```bash
python3 -m mantis build              # fetch (cached in data/raw/), score, write outputs
python3 -m mantis build --refresh    # re-download everything (do this the morning of your draft)
python3 -m mantis top -n 60                          # 9-cat, 12 teams
python3 -m mantis top --format points -n 40          # ESPN points scoring
python3 -m mantis top --punt ftp to --teams 10       # punt FT% and TO
python3 -m mantis top --pos C -n 20
open dist/draft-mantis.html          # interactive draft board
```

Outputs:

| File | What |
|---|---|
| `dist/draft-mantis.html` | Self-contained draft board: format/teams/punt toggles, tiers, edge vs ADP, mark picks as Mine/Taken, team category profile, "best fit" suggestions |
| `data/players.json` | Merged + scored player table (feeds the board) |
| `data/rankings_9cat.csv` | Default 12-team 9-cat rankings for spreadsheets |

## Roster-aware recommendations

Every recommendation respects your league's roster:

- **Position limits** (ESPN `rosterSettings.positionLimits`, counted by a player's *primary* position; ESPN's default caps centers at 4). Once you hit a cap, players at that position are blocked: dimmed, sorted to the bottom, no Mine/Draft button, never recommended or queued.
- **Starting lineup** (ESPN `lineupSlotCounts`: default PG, SG, SF, PF, C, G, F, 3 UT, 3 bench). Your picks are fitted into starting spots (multi-position players are placed where they help most). Each available player gets a chip: the open spot he would fill (`PG`, `F`, `UT`…), `Bench`, `No slot`, or `Max C`.
- **Ranking**: with **Rank for my roster** on (default), available players are ordered by Mantis score plus a bonus for filling an open starting spot (bigger for a specific spot like C than for UT, growing as the draft goes on), minus a penalty for bench-only picks. When your remaining picks equal your open starting spots, only players who fill one are recommended.
- The rail shows your lineup with open spots and position counts against limits (`C 4/4` turns red). Team cards in the League panel show each opponent's counts.
- Strike now, Best fit, Can wait on, the ESPN queue, "Mantis says", mock autopick and the bots all use the same rules. Mock drafts refuse a pick that breaks a limit, as ESPN does.

When your ESPN league is connected, its lineup and limits are used. Otherwise set them in **Roster rules** (defaults to ESPN's standard roster with max 4 C).

## Mock draft (practice)

Click **Mock draft** on the board (works in the hosted link and locally, no server needed). Choose teams, your slot (or random), rounds, opponent style, bot speed and an optional pick clock. Scoring follows the board's current format and punts, or your connected league's settings.

- Snake draft against bots: **ADP followers** (ESPN ADP + noise), **value hunters** (Mantis rank + noise) and **wildcards** (bigger reaches). Bots avoid stacking one position and fade injured players.
- On your turn the bar turns green and every available row gets a **Draft** button (also in the player card). If the clock runs out, Mantis picks for you.
- **Undo my last pick**, **Autodraft the rest**, **Restart**, **Exit mock**. A mock in progress survives a page reload.
- **Results**: letter grade and projected finish from a round-robin of full-season category totals (every league category, including ones you punted) or season fantasy points; where you win and lose each category; every pick vs. its ADP; best value and biggest reach.

Grades come from the same projections the Mantis Score uses, so they show how well you drafted *by this model*, not a guarantee.

## Connect your ESPN league (live draft sync)

```bash
python3 -m mantis serve          # opens http://localhost:8765 — the board plus live league sync
```

Click **Connect ESPN league** on the board, then:

1. **Cookies (private leagues, and to find your leagues).** Logged in to espn.com, open DevTools → Application (Chrome) / Storage (Safari, Firefox) → Cookies → `espn.com`. Copy `SWID` and `espn_s2`. Public leagues don't need them.
2. **Find my NBA leagues** lists the fantasy basketball leagues on your account (ESPN fan API), or enter the league ID from your league URL (`leagueId=…`).

Once connected the board:

- uses your league's categories (or custom points scoring), team count and roster size; uncheck **Use league settings** to override
- polls ESPN every 4 s during the draft (15 s before it starts, 60 s after) and marks every player Yours / team abbrev / Available
- tracks the snake order: who's on the clock, your next two picks, "You are on the clock" alert (bar turns green, tab title changes)
- "Can wait on" suggests the best player whose ADP says he'll survive until your pick after next
- shows every team's roster and category profile; after the draft, rosters keep syncing (trades, pickups)
- **Refresh projections** re-pulls ESPN / Basketball-Reference / Sleeper without restarting

### Making picks

Picks are made in ESPN's own draft room. ESPN has no API for submitting draft picks: the draft room runs on a members-only live connection, and Draft Mantis deliberately does not impersonate it. The board hands off instead:

- **Open ESPN draft room** opens your league's draft room in a new tab (keep it side by side with the board).
- **Copy** on any available player, or **Copy name** next to Mantis's pick when you're on the clock, puts the name on your clipboard: paste into ESPN's player search, press Draft.
- **Set your ESPN queue** lists Mantis's next six targets with **Copy list**. ESPN autopicks from your queue if your clock runs out, so keep it loaded.

### Firefox add-on: Draft Mantis Bridge (live picks)

ESPN's league API can report nothing mid-draft (picks show `playerId -1`, rosters stay empty until the draft ends). The add-on in `firefox-addon/` fixes that: it listens to the ESPN draft room open in **your** Firefox and passes each pick to `mantis serve` the moment it happens. The server pushes it to the board over an event stream, so the board updates instantly and shows the on-the-clock team and countdown from the draft room itself.

It is **read-only**:

- `tap.js` loads before ESPN's code and adds a passive listener to the draft room's connection to `fantasydraft.espn.com`. It never calls `send()`, never opens a connection, and ignores every other socket.
- `relay.js` keeps only draft command lines (`SELECTING <team> <ms>`, `SELECTED <team> <player> <n>`); chat and everything else is dropped. The socket's query string, which carries your member security code, is never forwarded.
- `background.js` posts those lines to `http://localhost:<port>/api/draftfeed`; nothing leaves your computer. The server accepts extension requests on that one endpoint only.

**Install**

1. Quick (removed when Firefox restarts): open `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** → pick `firefox-addon/manifest.json`.
2. Permanent: release Firefox only installs signed add-ons. Sign it privately (unlisted, not published) with a free addons.mozilla.org account:
   `cd firefox-addon && npx web-ext sign --channel=unlisted --api-key=<JWT issuer> --api-secret=<JWT secret>` and open the resulting `.xpi` in Firefox. Or, in Firefox Developer Edition / Nightly, set `xpinstall.signatures.required` to `false` in `about:config` and install `dist/draft-mantis-bridge-1.0.0.xpi` from `python3 -m mantis addon`.
3. Click the toolbar icon → **Allow** (Firefox asks before an add-on can read ESPN pages or reach localhost). The popup shows whether the board is running, whether a draft room is connected, and how many picks it has seen.

**Before draft day:** start `python3 -m mantis serve`, connect your league, open an ESPN mock draft (fantasy.espn.com → Mock Draft Lobby) and check the popup counts picks. Open or reload the draft room *after* the add-on is installed: it has to load before ESPN's code. ESPN replays the whole draft to a rejoining client, so a reload mid-draft catches up.

**Known gaps:** the feed format is undocumented and observed by the community, not ESPN; a commissioner undo is not handled (its message has not been observed); auction nominations and bids are not parsed. If ESPN changes the format, the popup's line count keeps rising while picks stay at 0; the board then falls back to ESPN's API.

Lint: `npx web-ext lint --source-dir firefox-addon` (0 errors, 0 warnings). Chain test: run `python3 -m mantis serve --no-open`, then `node tests/addon_chain.test.mjs`.

### Sync speed

With the add-on, picks arrive instantly. Without it, the board polls ESPN's league API every 2.5 s during the draft, 1.5 s when your pick is within two, 3 s in the 10 minutes before the scheduled start, 15 s otherwise. A pick normally shows up within about 2-4 s of ESPN's own draft room. ESPN's read API can lag the draft room further; the bar shows the time of the last update.

Terminal equivalents: `python3 -m mantis login`, `python3 -m mantis leagues`, `python3 -m mantis league --id 1234567`.

**Security:** `espn_s2` is a login session token for your ESPN account. It is stored only in `~/.draft-mantis/espn.json` (mode 600) or read from `ESPN_SWID` / `ESPN_S2` env vars, sent only to ESPN, and never returned to the browser. The server binds to 127.0.0.1 and rejects foreign `Host` / `Origin` headers. Remove with **Forget cookies** or delete the file. The shared (hosted) copy of the board cannot reach ESPN; live sync only works from `mantis serve`.

The ESPN endpoints are unofficial and undocumented (`lm-api-reads.fantasy.espn.com/apis/v3/games/fba/...` and `fan.api.espn.com/apis/v2/fans/{SWID}`); ESPN can change them without notice.

Tests (offline, fake league): run `python3 -m mantis build` once (it creates `data/`, which is not in the repo), then `python3 -m unittest discover tests`

## Season outlook (after the draft)

```bash
python3 -m mantis season --sims 2000   # terminal report
python3 -m mantis serve                # board → Season tab
```

Needs a connected ESPN league. For each team: current record, projected W-L-T, **strength** (chance to beat an average opponent in a typical week), **playoff %** and **title %**, and its league rank in every category. For your team: category win rates, a game plan, trade ideas, waiver add/drops and this week's streamers.

**How it works**

1. **Player lines**: preseason projection blended with this season's stats; real stats weigh 50% after 15 games and 75% after 45 (`CUR_HALF_GAMES` in `config.py`). Players 20% above / below their projection over the last 15 days are flagged Hot / Cold.
2. **Weekly team totals**: ESPN's NBA schedule gives every team's game days. For each day the best players with a game fill your starting spots (ESPN only counts the active lineup), weighted by availability (out players count zero this week and next).
3. **Randomness**: week-to-week variance per category comes from the G-score paper ([Rosenof, arXiv 2307.02188](https://arxiv.org/abs/2307.02188), Table 8); a per-season projection-error term (shrinks as the season goes on) keeps early odds honest.
4. **Simulation**: the league's real remaining schedule is played out 1,000-2,000 times (each-category, most-categories and points scoring), then a seeded playoff bracket with byes.
5. **Trades**: every 1-for-1 and 2-for-2 with each team is scored by the change in both teams' weekly win rate against the whole league. Kept only if it helps you, does not hurt them (so it can get accepted), and leaves both rosters legal (position limits, full lineup). "Helps both teams" marks real win-wins, usually you trading a surplus category for one you need. **Copy offer** gives text to send in the ESPN app.
6. **Waivers**: best add/drop pairs among players no one rosters. **Streamers**: unrostered players with the most games left this matchup.
7. **Game plan**: punt candidates (categories you win under 30% of weeks), swing categories (40-60%: small upgrades flip matchups), injured players to move to IR, games-volume gaps, sell-high and buy-low names once stats exist.

### Your moves (tracking + what-if)

The top of the Season tab follows what you actually do in ESPN:

- **League moves**: your waiver claims, free-agent adds, drops and trades from ESPN (`mTransactions2` + `mPendingTransactions`), with status (Pending, with the time ESPN processes it / Done / Didn't go through), the estimated change in your weekly win rate, and a **Mantis pick** tag when the move follows a suggestion shown *before* you made it. Every transaction seen is kept in `data/transactions.json`, since ESPN only returns a recent window.
- **Mantis suggestions**: each waiver and trade idea you were shown (kept 21 days in `data/plan.json`) marked Open / Pending / Partly done / Done.
- **Scenarios** (same random draws in each, so differences come from the rosters): *Draft-day roster* (your completed moves undone), *Now* (ESPN rosters; an empty spot while a claim is pending shows up as a dip), *If pending moves clear*, *Plus remaining suggestions* (the open ones that still help the roster you'll have, best first).
- **What-if**: stack add/drops and trades (any partner, any players) on top of your pending moves and simulate. Warns when a move breaks a position limit or overfills the roster. Nothing is sent to ESPN.
- **Odds history**: a line of your playoff odds, one point per season report (daily refresh + manual runs), in `data/history.jsonl`.

The tab checks ESPN every minute while open. When a claim or trade goes through, it says so and re-runs the season with the new rosters. Suggestions are generated on the roster you'll have once your pending moves clear, so the same move is never suggested twice.

## Daily data refresh

```bash
python3 -m mantis schedule install [--at 07:30]   # macOS launchd job, runs `python3 -m mantis daily`
python3 -m mantis schedule status
python3 -m mantis schedule remove
```

`daily` re-pulls ESPN (projections, this season's stats, last 15 days, injuries, ADP), Basketball-Reference and Sleeper, rebuilds scores, refreshes ESPN's NBA schedule and the season report. A run missed while the Mac sleeps fires on wake. Log: `data/logs/daily.log`. An open board (`mantis serve`) checks every 10 minutes and reloads itself when new data lands (or tells you, if a dialog or mock draft is open). The hosted copy of the board is a snapshot and only updates when republished.

## Data sources

| Source | Used for | Notes |
|---|---|---|
| ESPN Fantasy API (`lm-api-reads.fantasy.espn.com`, `fba`) | 2026-27 projections, 2025-26 actuals, ADP, auction $, injury status, season outlook | Public, no key. Season id 2027 = 2026-27 |
| Basketball-Reference season pages | Games played 2024-25 / 2025-26 (durability), age, usage %, BPM | Rate limit ~20 req/min; fetcher sleeps 4s |
| Sleeper API (`api.sleeper.app/v1/players/nba`) | Birth dates, years of experience | Public, no key |
| stats.nba.com | Not used | Blocks non-browser clients (requests time out) |

## How the Mantis Score works

1. **Scored line** = 70% ESPN 2026-27 projection + 30% 2025-26 actuals (projection only for rookies and players who missed last season).
2. **Category value**: z-score per 9-cat stat against the draftable pool (teams x 13). FG%/FT% are volume-weighted (attempts x (pct - pool pct)). TO is negative. Points leagues use ESPN default scoring.
3. **Replacement level** = value of the last rosterable player. VORP = value - replacement.
4. **Season value** = VORP x availability x age curve x health. Availability = (expected games / 72)^0.7, where expected games blends ESPN projected GP (60%) with 2-season history (40%).
5. **Mantis Score** = 50 + 50 x value / best value. 100 = best player, 50 = replacement.
6. **Edge** = ESPN ADP - Mantis rank. Positive = you can likely wait; negative = you'd be reaching.

All knobs live in `mantis/config.py`. The board's JS `scoreAll()` mirrors `model.score()`; keep them in sync if you change the formula.
