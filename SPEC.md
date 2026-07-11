# Sharp Money Detector — Project Specification (FINAL)

**Hackathon:** Superteam World Cup Hackathon — Track 03 (Trading Tools and Agents)
**Deadline:** July 19, 2026
**Builder:** Solo
**Prize pool:** $10k / $4k / $2k

*This is the single source of truth for this project. Delete any older SPEC files — only this one is current.*

---

## ⚠️ Rules for accuracy in this document

Every endpoint, field name, and behavior marked **[CONFIRMED]** was directly tested with a real request and a real response during development — not assumed, not guessed. Anything marked **[TO VERIFY]** has NOT been confirmed and must be checked live before code is written against it.

You have the TxLINE docs MCP connected in your coding tool. When it fetches endpoint details, always ask it to show you the actual raw response before writing detection logic on top of it — never let it silently assume a schema. This document is the running record of what's actually been proven to work.

---

## 1. Project Overview

### What it is
An autonomous Python agent that:
1. Connects continuously to TxLINE's live odds feed for World Cup 2026 matches
2. Tracks how TxLINE's de-vigged sharp consensus price (StablePrice) moves over time — significant, persistent movement is itself a signal ("steam")
3. Near kickoff, pulls independent odds from Pinnacle (sharpest traditional bookmaker) and Polymarket (real-money prediction market) via OddsPapi, and checks whether all three sources agree or diverge on the same outcome
4. Scores every signal's confidence based on magnitude, persistence, cross-market agreement, and timing
5. Checks whether the same signal appears across multiple betting markets (1X2, Asian Handicap, Over/Under) simultaneously for higher-confidence alerts
6. Simulates a paper-trading bet on each signal and tracks hypothetical profit/loss over the tournament
7. Automatically fetches match results after full-time and scores every signal as correct or incorrect — no manual input required
8. Displays everything on a public, mobile-responsive web dashboard
9. Generates a plain-English explanation of each signal using Gemini AI

### Why this satisfies the judging criteria
| Criterion | How this project addresses it |
|---|---|
| Core Functionality & Data Ingestion | Agent runs on live TxLINE SSE feed plus real OddsPapi bookmaker data, not simulated data |
| Autonomous Operation | No manual triggers anywhere — stream in, detection, scoring, result-fetching, and dashboard updates all run unattended once deployed |
| Logic & Code Architecture | Steam detection and tri-source divergence are clean deterministic formulas; confidence scoring is a documented weighted formula; nothing is a black box |
| Innovation & Novelty | Combining TxLINE's de-vigged sharp consensus with Pinnacle AND Polymarket for tri-source divergence is not something any competitor researched so far has built |
| Production Readiness | Paper trading P&L + auto-scoring + a documented, rationed API budget shows the tool is built like a real constrained production system, not a toy demo |

### Why the original single-source idea changed
Testing showed TxLINE's odds snapshot endpoint returns only one bookmaker value: `TXLineStablePriceDemargined`. There is no second bookmaker inside that same response to compare against — so "sharp vs market divergence" could not be built from TxLINE alone. The fix: bring in OddsPapi, a free, independent 133-bookmaker aggregator that includes both Pinnacle and Polymarket. This gives three genuinely independent readings on the same outcome instead of one.

---

## 2. Architecture

```
TxLINE SSE Stream (unlimited, free)      OddsPapi (250 total calls, rationed)
        │                                         │
        ▼                                         ▼
  Steam/Movement Detection            Tri-Source Divergence Detection
  (runs continuously)                  (runs only ~2-3hrs before each kickoff)
        │                                         │
        └─────────────────┬───────────────────────┘
                           ▼
                Confidence Scoring
                           │
                           ▼
                  SQLite Database
                           │
                           ▼
                  Flask Backend API
                           │
                           ▼
        Frontend Dashboard (mobile-responsive)
        Live signals · History · P&L equity curve · Tri-source view · Stats
```

**Local development first:** everything runs on localhost — agent script in one terminal, Flask on port 5000, frontend opened directly or served via Flask. Deployment host decided only after the full local pipeline works end to end.

---

## 3. Confirmed API Details

### 3.1 TxLINE Authentication [CONFIRMED]
```
POST https://txline.txodds.com/auth/guest/start        → returns JWT
On-chain subscribe: Anchor program 9ExbZjAapQww1vfcisDmrngPinHTEfpjYRWMunJgcKaA (mainnet)
  SERVICE_LEVEL_ID = 1 (free tier: World Cup + Int'l Friendlies, 60-second delay)
POST https://txline.txodds.com/api/token/activate       → returns apiToken
```
Every subsequent TxLINE request requires BOTH headers:
```
Authorization: Bearer <jwt>
X-Api-Token: <apiToken>
```
Tokens live in `tokens.json` at the project root. **Never commit this file — add to `.gitignore` immediately.** If a token is ever pasted anywhere public (chat, forum, screenshot), regenerate immediately by re-running the activation script.

### 3.2 TxLINE Fixtures Snapshot [CONFIRMED]
```
GET https://txline.txodds.com/api/fixtures/snapshot?startEpochDay={epochDay}
```
Returns an array of fixture objects:
```json
{
  "Ts": 1783339200000,
  "StartTime": 1783299600000,
  "Competition": "World Cup",
  "CompetitionId": 72,
  "FixtureGroupId": 10115574,
  "Participant1Id": 2545,
  "Participant1": "Mexico",
  "Participant2Id": 1888,
  "Participant2": "England",
  "FixtureId": 18192996,
  "Participant1IsHome": true,
  "GameState": 3
}
```
`epochDay` = days since Jan 1 1970 (`(date.today() - date(1970,1,1)).days`).

### 3.3 TxLINE Odds Snapshot [CONFIRMED]
```
GET https://txline.txodds.com/api/odds/snapshot/{fixtureId}
```
Returns an array of odds ticks. **Every item's `Bookmaker` field is `"TXLineStablePriceDemargined"`** — this endpoint gives you the sharp consensus only, confirmed via live test on a real fixture (USA vs Belgium, FixtureId 18193785). There is no second bookmaker in this response — that's exactly why OddsPapi was added.

Confirmed real sample:
```json
{
  "FixtureId": 18193785,
  "MessageId": "1836593456:00003:000029-10021-stab",
  "Ts": 1783348277224,
  "Bookmaker": "TXLineStablePriceDemargined",
  "BookmakerId": 10021,
  "SuperOddsType": "1X2_PARTICIPANT_RESULT",
  "InRunning": false,
  "GameState": null,
  "MarketParameters": null,
  "MarketPeriod": "half=1",
  "PriceNames": ["part1", "draw", "part2"],
  "Prices": [3296, 2356, 3673],
  "Pct": ["30.340", "42.445", "27.226"]
}
```
Confirmed observations:
- `SuperOddsType` is the market type field. Confirmed values seen: `1X2_PARTICIPANT_RESULT`, `ASIANHANDICAP_PARTICIPANT_GOALS`, `OVERUNDER_PARTICIPANT_GOALS`. Multiple market types exist per fixture — this is what makes cross-market correlation buildable from TxLINE data alone.
- The SAME fixture + market shows DIFFERENT `Prices` at different `Ts` timestamps (confirmed: one snapshot `[3296, 2356, 3673]`, another `[2567, 3630, 2985]` for the same 1X2 market). This price movement over time is the core "steam" signal.
- `Pct` appears to already be a de-vigged percentage (values sum to ~100 for 1X2 markets) — confirmed from sample: 30.340 + 42.445 + 27.226 ≈ 100.
- `Prices` scaling — **[CONFIRMED]** — `Prices` values are **decimal odds × 1000**. Divide by 1000 to get usable decimal odds. Divide that into 1 to get implied probability. Proven live against Spain vs Belgium (2026-07-10):
  - `Prices: [2305, 2484, 6110]` → decimal odds `[2.305, 2.484, 6.110]` → implied probs `[43.384%, 40.258%, 16.367%]`
  - These match `Pct: ["43.384", "40.258", "16.367"]` exactly. Raw÷100 gives 108.99% total (wrong). 1÷(raw÷1000) gives 100.008% (correct, de-vigged).
  - **In code:** `decimal_odds = price / 1000`, `implied_prob_pct = (1 / decimal_odds) * 100`
  - `Pct` is only populated for 1X2 markets. Asian Handicap and Over/Under return `Pct: ["NA", "NA"]` — use the formula above for those markets.


### 3.4 TxLINE Odds SSE Stream [CONFIRMED]
```
GET https://txline.txodds.com/api/odds/stream
```
Auth: **headers only** (same as all REST endpoints — no query-param auth for SSE):
```
Authorization: Bearer <jwt>
X-Api-Token: <apiToken>
Accept: text/event-stream
Cache-Control: no-cache
```
Optional query param: `?fixtureId={id}` — filters stream to a single fixture.

Reconnection/resume: send `Last-Event-ID: <string>` header — the ID of the last received event — to resume from that point.

Two event types on the stream:
1. **Data messages** — `id` in the format `timestamp:index`; `data` = JSON of a single `OddsPayload` object (identical schema to the snapshot endpoint, including `Pct`).
2. **Heartbeats** — `event: heartbeat`; data like `{"Ts": 12345}`.

Confirmed `OddsPayload` fields (from OpenAPI v1.5.2):
```json
{
  "FixtureId":        1234567,
  "MessageId":        "string",
  "Ts":               1783348277224,
  "Bookmaker":        "string",
  "BookmakerId":      10021,
  "SuperOddsType":    "string",
  "GameState":        "string (optional)",
  "InRunning":        false,
  "MarketParameters": "string (optional)",
  "MarketPeriod":     "string (optional)",
  "PriceNames":       ["part1", "draw", "part2"],
  "Prices":           [3296, 2356, 3673],
  "Pct":              ["30.340", "42.445", "27.226"]
}
```
`Pct` pattern per spec: `^(NA|\d+\.\d{3})$` — 3 decimal places, or `NA` for quarter-handicap lines.

### 3.5 TxLINE Scores Feed [CONFIRMED]
Three endpoints (all require same `Authorization` + `X-Api-Token` headers):
```
GET https://txline.txodds.com/api/scores/snapshot/{fixtureId}    → latest score per action (live)
GET https://txline.txodds.com/api/scores/updates/{fixtureId}     → all updates in current 5-min window
GET https://txline.txodds.com/api/scores/historical/{fixtureId}  → full sequence post-match
GET https://txline.txodds.com/api/scores/stream                  → SSE stream (same auth + Last-Event-ID resume)
```
⚠️ `/api/scores/historical/{fixtureId}` only returns data for fixtures whose start time is **between two weeks and six hours in the past** — this is the correct endpoint for Feature 4.6 auto result fetching.

**[CONFIRMED 2026-07-09 via probe_scores_deep.py]** The historical endpoint returns **SSE format** (`Content-Type: text/event-stream`), not a JSON array. Each line of the form `data: {...}` is a JSON score update object.

Confirmed **actual field names** (PascalCase throughout — the OpenAPI spec lowercase names are wrong):
```
Scores.GameState         string   — game phase (see below)
Scores.Action            string   — event type: "game_finalised", "goal", "comment", etc.
Scores.Seq               integer  — sequence number
Scores.Ts                integer  — millisecond timestamp
Scores.Score             object   — score data (present on action events; absent on metadata events)
  └─ Participant1        object
  └─ Participant2        object
       └─ Total.Goals    integer  ← full-match goals  ← USE THIS for winner determination
       └─ H1.Goals       integer  ← first-half goals
       └─ H2.Goals       integer  ← second-half goals
```

> ⚠️ **The field is `Score` NOT `scoreSoccer`** — the SPEC.md initial assumption was wrong. Confirmed on two finished fixtures.

**"Match finished" detection — [CONFIRMED]:**
The API does NOT reliably set `GameState` to `"F"/"FET"/"FPE"` for these World Cup fixtures. The reliable finish signal is:
```
Action == "game_finalised"
```
The `Score` field on the `game_finalised` entry contains the definitive final tally.
Use:
```python
final_entry = next(
    (u for u in reversed(updates) if u.get("Action") == "game_finalised"),
    None,
)
```

**Results confirmed from live probes (2026-07-09):**
| Fixture | Participant1 | Participant2 | Score | Winner |
|---|---|---|---|---|
| USA vs Belgium (18193785) | USA | Belgium | 1–4 | `part2` |
| Argentina vs Egypt (18202701) | Argentina | Egypt | 3–2 | `part1` |

For Feature 4.6, look for `Action == "game_finalised"` in the historical SSE stream and read `Score.Participant1.Total.Goals` vs `Score.Participant2.Total.Goals` to determine `part1 / draw / part2`.


### 3.6 OddsPapi Authentication & Base [CONFIRMED]
```
Base URL: https://api.oddspapi.io/v4
Auth: ?apiKey={your_key} as a query parameter on every request
Free tier limit: 250 requests TOTAL for the account — not per day, TOTAL
```
**This is the single tightest constraint in the whole project.** Every call must be deliberate.

### 3.7 OddsPapi Fixtures [CONFIRMED]
```
GET /fixtures?apiKey={key}&sportId=10&from={date}&to={date}
```
Confirmed real response includes: `fixtureId`, `participant1Name`, `participant2Name`, `tournamentName`, `startTime`, `hasOdds` (false once match ends — confirmed on a completed USA vs Belgium fixture).

### 3.8 OddsPapi Odds [CONFIRMED]
```
GET /odds?apiKey={key}&fixtureId={fixtureId}
```
Confirmed real response structure:
```json
{
  "fixtureId": "id1000001653452521",
  "hasOdds": true,
  "bookmakerOdds": {
    "pinnacle": { "bookmakerIsActive": true, "markets": { "102041": {
      "outcomes": { "102041": { "players": { "0": { "price": 2.94, "priceAmerican": "194" } } } }
    } } },
    "polymarket": { "bookmakerIsActive": true, "markets": { "{market_id}": {
      "outcomes": { "{outcome_id}": { "players": { "0": {
        "price": 1.587,
        "exchangeMeta": { "back": [ { "cents": 0.63, "price": 1.587, "limit": 248.91, "size": 607.1 } ] }
      } } } }
    } } }
  }
}
```
Confirmed: **133 bookmakers available**, including `pinnacle`, `bet365`, `polymarket`, `kalshi`, `fanduel`, `draftkings`.

**Upcoming World Cup fixture OddsPapi IDs confirmed 2026-07-08:**
- `id1000001653452525` — France vs Morocco (2026-07-09)
- `id1000001653452527` — Spain vs Belgium (2026-07-10)
- `id1000001653452529` — Norway vs England (2026-07-11)

**Pinnacle field path [CONFIRMED]:**
- `bookmakerOdds.pinnacle.markets.{market_id}.outcomes.{outcome_id}.players.0.price` → **decimal odds**
- No `marketName` field present — market must be identified by outcome count (3 outcomes = 1X2) and `mainLine=true` flag
- De-vig: `implied_raw[i] = 1 / price[i]`, `overround = sum(implied_raw)`, `fair_prob[i] = implied_raw[i] / overround`
- For knockout fixtures (no draw possible in Polymarket), use only `home` and `away` outcomes; draw outcome treated as Pinnacle-only

**Polymarket field path [CONFIRMED]:**
- Polymarket is a **prediction market exchange**, NOT a standard bookmaker — do NOT use `price` field
- `bookmakerOdds.polymarket.markets.{market_id}.outcomes.{outcome_id}.players.0.exchangeMeta.back[0].cents` → **implied probability on 0–1 scale** (e.g. `0.63` = 63%)
- Multiply by 100 to get a percentage for comparison with TxLINE and Pinnacle
- **Do NOT de-vig Polymarket** — `cents` is already a direct market probability
- Confirmed real values from Spain vs Belgium: one outcome `cents=0.63`, another `cents=0.45`
- Polymarket only has binary outcomes for knockout matches (team A to advance / team B to advance) — no draw market

**Market ID identification strategy [CONFIRMED]:**
- No `marketName` field present in live response — must be inferred from structure
- Pinnacle 1X2 match result: find markets with exactly 3 outcomes where at least one has `mainLine=true`
- Polymarket: typically has 2 outcomes (one per team) for knockout round "to advance" markets; use `mainLine=true` for the main line

### 3.9 OddsPapi Request Budget — Critical Constraint
250 total requests, project has until July 19. **Cannot poll on a timer.** Strategy:
- One `/odds` call per fixture, roughly 2-3 hours before kickoff (pre-match lines are settled and most comparable across sources)
- Optionally one more call very close to kickoff if budget allows
- Every call must be logged in the `oddspapi_usage` table (see schema below) so remaining budget is always known
- If budget is close to running out, prioritize marquee/high-profile fixtures over lower-interest ones

---

## 4. Signal Detection Logic

### 4.1 TxLINE-only Steam/Movement Detection
```
for each (fixture_id, market_type) pair:
    maintain a rolling window of (Ts, Prices) ticks from TxLINE snapshot/stream

    on new tick:
        pct_change = (new_price - previous_price) / previous_price * 100  [per outcome]
        if abs(pct_change) >= MOVEMENT_THRESHOLD_PCT:
            persistence = count of consecutive prior ticks moving the same direction
            if persistence >= PERSISTENCE_MIN_TICKS:
                log a STEAM signal:
                    fixture_id, market_type, direction, pct_change,
                    persistence, detected_at, match_minute (if in-play)
```

### 4.2 Tri-Source Divergence Detection (the differentiator)
Runs once per fixture, in the 2-3hr pre-kickoff window (to conserve OddsPapi budget):
```
fetch TxLINE StablePrice implied probability for this fixture's 1X2 market
    (already de-vigged — use the Pct field directly, confirmed to sum to ~100)

fetch OddsPapi Pinnacle raw decimal odds for the same fixture/market
    de-vig manually:
        implied_prob_raw[i] = 1 / decimal_odds[i]
        overround = sum(implied_prob_raw)
        pinnacle_fair_prob[i] = implied_prob_raw[i] / overround

fetch OddsPapi Polymarket price for the same fixture/market
    [CONFIRMED] use exchangeMeta.back[0].cents (0–1 scale); multiply by 100 for %; do NOT de-vig
    NOTE: Polymarket only has binary outcomes for knockout matches (no draw market)
          → compare only home/away; treat draw as TxLINE+Pinnacle-only outcomes

for each outcome (home/draw/away):
    values = [txline_prob, pinnacle_fair_prob, polymarket_prob]
    max_divergence = max(values) - min(values)
    if max_divergence >= DIVERGENCE_THRESHOLD_PCT:
        identify which source is the outlier (furthest from the other two)
        log a TRI_SOURCE_DIVERGENCE signal:
            fixture_id, outcome, txline_prob, pinnacle_prob, polymarket_prob,
            max_divergence, outlier_source, detected_at
```

### 4.3 Cross-Market Correlation
Already buildable from TxLINE alone since multiple `SuperOddsType` values are confirmed available per fixture.
```
on a new STEAM signal for fixture X, market type M:
    check for other STEAM signals on fixture X, different market type,
    within the last 10 minutes
    if 2+ distinct market types show aligned-direction signals:
        group them under a shared cross_market_group_id
        flag as HIGH_CONFIDENCE_ALERT
```

### 4.4 Confidence Scoring
```
magnitude_score = min(divergence_or_movement_pct / 15.0, 1.0)
persistence_score = min(persistence_ticks / 10.0, 1.0)
source_agreement_score = (number of sources agreeing on direction, for tri-source signals) / 3
                          (use 1.0 for pure steam signals — only one source involved)
timing_score = 1.0 if pre-match else 0.6 if match_minute < 60 else 0.3

confidence = (magnitude_score * 0.35) +
             (persistence_score * 0.25) +
             (source_agreement_score * 0.25) +
             (timing_score * 0.15)
```
**These weights are a reasoned starting point, not derived from real data.** Once signals accumulate over a day or two of live running, revisit the numbers and be ready to justify your final choice to judges — "why 0.35 for magnitude" needs a real answer, not "the AI picked it."

### 4.5 Paper Trading P&L Simulation
```
on any signal (STEAM or TRI_SOURCE):
    stake = 100  (flat) — or scale 0.5x-2x by confidence_score, decide and document which
    record: signal_id, stake, odds_taken (the sharp/TxLINE price), direction, status = "OPEN"

on fixture result confirmed (see 4.6):
    if direction matches actual result:
        profit_loss = stake * (odds_taken - 1); status = "WON"
    else:
        profit_loss = -stake; status = "LOST"
    update record, recompute cumulative P&L for the equity curve
```

### 4.6 Auto Result Fetching
**Blocked until 3.5 [TO VERIFY] is confirmed.**
```
every RESULT_CHECK_INTERVAL_SEC (e.g. 60s):
    for each fixture in fixtures_tracked where status in (UPCOMING, LIVE):
        if now() > fixture.start_time + estimated_match_duration:
            result = fetch_final_score(fixture.fixture_id)   # endpoint TBC, section 3.5
            if result is available:
                mark fixture FINISHED, store final_result
                for each signal logged against this fixture:
                    outcome = "CORRECT" if signal.direction == result.winner else "INCORRECT"
                    update signal.outcome, signal.scored_at
                    settle corresponding paper_trade (4.5)
```
This loop is what makes the whole system "autonomous" per the judging criteria — no manual step anywhere from stream event to scored, settled outcome.

### 4.7 AI Commentary [CONFIRMED — on-demand, not automatic]

**Design decision:** Commentary is generated on-demand via a dedicated API endpoint only. It is NOT wired into the signal detection loop. This keeps the detection loop fast, avoids any Gemini latency on every signal, and respects the limited API quota.

**How it works:**
```
POST /api/signals/<signal_id>/commentary
  → if ai_commentary already in DB: return cached (no Gemini call)
  → else: call Gemini, save to DB, return result
  → if key not set: 503 {"error": "GEMINI_API_KEY not configured"}
  → if signal not found: 404
```

**Key resolution** (in priority order):
1. `GEMINI_API_KEY` environment variable (SPEC.md preferred)
2. `gemini_key` field in `tokens.json` (project-local fallback, consistent with other modules)

**Prompt branches** (verbatim from original spec):
```python
# TRI_SOURCE_DIVERGENCE
"A tri-source divergence signal was detected in a World Cup match.\n"
"Match: {p1} vs {p2}\n"
"TxLINE StablePrice implied probability: {txline_prob}%\n"
"Pinnacle (de-vigged) implied probability: {pinnacle_prob}%\n"
"Polymarket implied probability: {polymarket_prob}%\n"
"Outlier source: {outlier_source}\n"
"Confidence score: {confidence_score}\n\n"
"Write one clear, specific sentence explaining what this divergence "
"means for a sports trader. No fluff, no hedging language."

# STEAM
"A sharp money steam signal was detected.\n"
"Match: {p1} vs {p2}\n"
"Market: {market}\n"
"Price moved {pct_change}% in the sharp consensus, "
"held for {persistence_ticks} consecutive updates.\n"
"Confidence score: {confidence_score}\n\n"
"Write one clear, specific sentence explaining what this movement "
"means for a sports trader. No fluff, no hedging language."
```

**Model:** `gemini-2.0-flash`

**Failure handling:** `generate_commentary()` catches all exceptions and returns `None` — the Flask route distinguishes "key not set" (503) from "call failed" (500). The detection loop is never affected.

**Files:**
- `agent/commentary.py` — `generate_commentary(signal)`, `_build_prompt(signal)`, `_resolve_api_key()`
- `backend/app.py` — `POST /api/signals/<int:signal_id>/commentary`
- `agent/database.py` — `get_signal_by_id(id)`, `update_signal_commentary(id, text)`


---

## 5. Database Schema (SQLite)

### `signals`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| fixture_id | TEXT | |
| competition | TEXT | |
| participant1 | TEXT | |
| participant2 | TEXT | |
| signal_type | TEXT | `STEAM` or `TRI_SOURCE_DIVERGENCE` |
| market | TEXT | e.g. `1X2_PARTICIPANT_RESULT` |
| txline_prob | REAL | nullable |
| pinnacle_prob | REAL | nullable, only for tri-source signals |
| polymarket_prob | REAL | nullable, only for tri-source signals |
| pct_change | REAL | for steam signals |
| divergence_pct | REAL | for tri-source signals |
| direction | TEXT | which outcome the signal favors |
| outlier_source | TEXT | nullable |
| confidence_score | REAL | 0–1 |
| persistence_ticks | INTEGER | |
| detected_at | TEXT | ISO timestamp |
| match_minute | INTEGER | nullable |
| ai_commentary | TEXT | |
| cross_market_group_id | TEXT | nullable |
| outcome | TEXT | nullable until scored: `CORRECT` / `INCORRECT` |
| scored_at | TEXT | nullable |

### `paper_trades`
`id, signal_id (FK), stake, odds_taken, status (OPEN/WON/LOST), profit_loss, settled_at`

### `fixtures_tracked`
`fixture_id (PK), competition, participant1, participant2, start_time, status (UPCOMING/LIVE/FINISHED), final_result, last_oddspapi_call_at, last_checked_at`

### `oddspapi_usage`
`id, called_at, endpoint, fixture_id, requests_remaining_estimate` — every OddsPapi call gets logged here immediately. Query this before making any new call to confirm budget remains.

**➡️ CHECKPOINT 1: Implement `agent/database.py` with this schema: `init_db()`, `insert_signal()`, `insert_paper_trade()`, `log_oddspapi_call()`, `get_oddspapi_usage_count()`, `get_open_fixtures()`, `mark_fixture_finished()`, `score_signal()`, `settle_paper_trade()`, `get_all_signals()`, `get_pnl_summary()`.**

---

## 6. Configuration — `agent/config.py`
```python
MOVEMENT_THRESHOLD_PCT = 5.0          # TxLINE steam detection — tune after seeing real data
PERSISTENCE_MIN_TICKS = 3
DIVERGENCE_THRESHOLD_PCT = 5.0        # tri-source divergence
STAKE_PER_BET = 100
RESULT_CHECK_INTERVAL_SEC = 60
RECONNECT_DELAY_SEC = 5
ODDSPAPI_TOTAL_BUDGET = 250
ODDSPAPI_PRE_MATCH_WINDOW_HOURS = 3   # when to make the single OddsPapi call per fixture
```
Document any changes to these numbers with your reasoning — judges may ask why.

---

## 7. Backend API — `backend/app.py` (Flask)

| Route | Method | Returns |
|---|---|---|
| `/api/signals` | GET | all signals, paginated (`?limit=&offset=`) |
| `/api/signals/live` | GET | signals from in-play fixtures only |
| `/api/signals/<fixture_id>` | GET | all signals for one fixture |
| `/api/pnl` | GET | cumulative P&L time series for equity curve |
| `/api/stats` | GET | overall accuracy %, high-confidence accuracy %, total signal count |
| `/api/fixtures` | GET | fixtures tracked and their status |
| `/api/usage` | GET | OddsPapi requests used / remaining |

All routes return JSON, CORS enabled for the frontend origin.

**➡️ CHECKPOINT 2: Build Flask app with these routes reading from SQLite. Test each manually (browser or curl) before touching the frontend.**

---

## 8. Frontend — `frontend/index.html`

### Sections
1. **Header** — project name, live/offline status (pings `/api/stats` every 30s), OddsPapi budget remaining
2. **Live Signals Feed** — auto-refreshing cards, most recent first. Each shows: match, signal type badge (STEAM vs TRI-SOURCE), direction, magnitude, confidence score, AI commentary, cross-market badge if applicable
3. **Tri-Source Comparison View** — for TRI_SOURCE signals specifically, show all three probabilities side by side (TxLINE / Pinnacle / Polymarket) — this is the visual proof of the differentiator, make it prominent
4. **Signal History Table** — sortable, filterable, outcome column (Correct/Incorrect/Pending)
5. **P&L Equity Curve** — Chart.js line chart from `/api/pnl`
6. **Accuracy Stats Panel** — overall win rate, high-confidence-only win rate, total signals

### Technical requirements
- Vanilla HTML/CSS/JS, no framework, Chart.js via CDN
- Mobile-responsive: single-column stacked layout under 768px, cards instead of table on small screens
- Polling every 15-30s is sufficient given the 60s TxLINE free-tier delay — no need for websockets

**➡️ CHECKPOINT 3: Build frontend against local Flask backend. Test on both desktop width and mobile width (browser dev tools responsive mode) before considering it done.**

---

## 9. Build Order (Do Not Skip Ahead)

1. ☐ Use Antigravity + TxLINE MCP to confirm 3.4 (SSE stream) and 3.5 (scores feed) with real responses — update this doc, change `[TO VERIFY]` to `[CONFIRMED]`
2. ☐ Confirm OddsPapi market ID → market name mapping and Polymarket's exact price format (3.8 TO VERIFY items)
3. ☐ **CHECKPOINT 1** — `database.py`
4. ☐ TxLINE steam detection (4.1) — terminal output only first, no DB yet
5. ☐ De-vig math function (4.2) — unit test with known odds values before wiring to live data
6. ☐ OddsPapi integration with strict budget tracking via `oddspapi_usage` table — test the logging works before making real calls count against the 250 limit
7. ☐ Tri-source divergence detection (4.2) wired to real data
8. ☐ Confidence scoring (4.4) + cross-market correlation (4.3)
9. ☐ Paper trading simulation (4.5)
10. ☐ Auto result fetching (4.6) — only once 3.5 is confirmed
11. ☐ **CHECKPOINT 2** — Flask backend
12. ☐ **CHECKPOINT 3** — Frontend, mobile-tested
13. ☐ Run agent continuously for 24+ hours accumulating real signals before recording demo
14. ☐ Tune `config.py` thresholds based on real observed data, document reasoning
15. ☐ Deployment (host decided after local pipeline fully works)
16. ☐ Record demo video, write submission documentation explaining the strategy in your own words

---

## 10. What You Must Personally Understand (Non-negotiable)

Regardless of what any AI tool generates, you must be able to explain from memory, without looking at code:
- Why TxLINE alone wasn't sufficient, and specifically what problem OddsPapi solves
- What de-vigging does mathematically and why it's necessary before comparing Pinnacle's raw odds to TxLINE's already-devigged StablePrice
- Why Pinnacle and Polymarket specifically were chosen as the second/third sources
- The confidence score formula and your reasoning for its weights
- Why the 250-request OddsPapi budget forced a pre-match-focused strategy — and why that's a legitimate design decision, not a limitation you're hiding from judges
- The complete data flow from a stream event or API call through to a scored, settled, dashboard-visible signal
- Why this counts as "autonomous operation" per the judging criteria

If you can't explain any of these in your own words, that part of the project isn't actually done — even if the code runs without errors.
