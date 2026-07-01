# RISEDUAL Mission Control — PRD

## Original Problem Statement
Connect separate AI project runtimes (Barracuda, GTO, Camino, Hellcat)
into one monorepo-style Mission Control backend. Enable real-money
trading pilot with Webull (equity) and Kraken Pro (crypto). 5-stage
pipeline execution, doctrine-aligned vocabulary, strict cash-account
trading, comprehensive provenance + health tracking.

### ⚠️ Webull MQTT Streaming — infrastructure shipped, awaiting entitlement confirmation (2026-07-02)

Full plumbing for Webull's MQTT tick-by-tick L1 stream is in place, defaulting OFF (`TRADER_EQUITY_STREAM_ENABLED=false`). Enable when operator confirms streaming entitlement on the OpenAPI plan.

- **`/app/trader/spread_stream.py`** — thread-based bridge wrapping the official `webull-python-sdk-mdata` `DefaultQuotesClient`. On QUOTE protobuf messages, extracts best `bids[0]` / `asks[0]`, updates the same `_latest` cache the HTTP poller writes to (`source="webull_mqtt"`), persists to SQLite. Auto-reconnect with exponential backoff.
- **`/api/admin/trader/spread`** now includes a `stream: {state, message_count, last_error, subscribed_symbols}` block.
- **SpreadWatcher.jsx** shows a `MQTT STREAM` chip (green when connected, amber starting, red on error) alongside message count.
- **Deps added** (via `pip install --no-deps` to bypass ancient grpcio pin): `webull-python-sdk-mdata`, `webull-python-sdk-quotes-core`, `webull-python-sdk-core`. Runtime uses env's newer `grpcio 1.81.1` + `protobuf 6.33.6` — verified binary-compatible with the SDK's protobuf schemas.
- **Tests**: 6 cases in `/app/backend/tests/test_trader_spread_stream.py`. All 33 total tests green.
- **Live-probe finding**: `data-api.webull.com` accepts TCP but the SDK's gRPC token-exchange returns `UNAVAILABLE: tcp handshaker shutdown` — coupling between MQTT host and gRPC Host metadata in the SDK. Root cause is likely: (a) streaming entitlement not active on the OpenAPI plan (Webull says market-data streaming is separately purchased and enabled), or (b) the operator's plan uses a different regional endpoint pairing. HTTP snapshot poller keeps flowing at 20s cadence in the meantime.


### ✅ Kraken + Webull Spread Poller (2026-07-02)

Live bid/ask spread telemetry for the sidecar trader with an optional
hard risk gate per lane. Same doctrine as the rest of the trader:
Mongo-free hot path, JSONL + SQLite truth tape, bounded timeouts.
- **`/app/trader/spread.py`** — two independent asyncio pollers:
  Kraken `/public/Ticker` for crypto pair(s) + Webull OpenAPI
  `/openapi/market-data/stock/snapshot` for equity (same-broker
  doctrine: quotes come from the same venue that gets the order).
- **`risk.check()`** — per-lane spread gate. Reads the in-memory cache
  (never blocks on I/O). **Fails OPEN on stale readings** so a dead
  poller cannot deadlock trading. Gate is env-flagged, defaults OFF.
- **`/api/admin/trader/spread`** — dashboard endpoint. Returns latest
  snapshot per symbol + rolling history + config surface.
- **`SpreadWatcher.jsx`** — Overview tile below TradeTape.
- Tests: `/app/backend/tests/test_trader_spread.py` (16 cases green).
- Live proof: preview backend produced 40+ Kraken XBTUSD ticks over
  ~10 min, spread stable around 0.02–0.28 bps.
- **✅ Webull L1 access working (2026-07-02)** — 2FA token creation flow now issues push notifications correctly. Root cause was three-layered: my original signing was based on a **third-party guide that was completely wrong**. Actual algorithm (verified byte-for-byte against `webull-inc/openapi-python-sdk`):
  1. `sign_params = { x-app-key, x-timestamp, x-signature-version, x-signature-algorithm, x-signature-nonce, host } ∪ query_params` (all keys lowercased)
  2. `body_string = MD5_hex_upper(compact_json(body))` if body else omitted
  3. `string_to_sign = URI + "&" + "&".join(sorted "k=v" pairs) [+ "&" + body_string]`
  4. `encoded = urllib.parse.quote(string_to_sign, safe="")` — URL-encode **everything** including `/` and `=`
  5. `signature = base64(HMAC-SHA1(app_secret + "&", encoded))` — note the trailing `&` on the secret
  6. **DO NOT send `x-app-secret` as a header** — Webull rejects with 401. Only 8 headers total.
- **Production base**: `api.webull.com` (UAT is `us-openapi-alb.uat.webullbroker.com`).
- **New endpoints**: `POST /api/admin/trader/webull-token-create` (triggers 2FA push), `GET /api/admin/trader/webull-token-status` (dashboard read).
- **Token persistence**: `/app/trader/data/webull_token.json` (survives future persistent-volume mount). Raw token never logged or surfaced over HTTP — only preview + length.
- **UI**: SpreadWatcher tile now has a "Webull Token" strip showing status (NONE / PENDING / NORMAL / EXPIRED), token preview, expires-in-hours, and an "Init Token" / "Reissue Token" button.
- Tests: 27/27 (added `test_webull_sign_matches_official_sdk_formula` — cross-verified against real SDK).

### ✅ Pass 3.5 — Frontend Rewire + Trade Tape Tile COMPLETED (2026-07-01)

Wired the new local-first backend into the operator dashboard:
- **`TradeTape.jsx`** — primary tile on Overview: trader status strip
  (enabled/alive/fires today/spent today/last cycle), lane filter,
  fired-only toggle, and a dense 15-row per-cycle table (time · lane ·
  symbol · executor · verdict · confidence · risk reason · broker
  result). 15s auto-refresh, reads `/api/admin/trader/{status,receipts}`.
- **`TraderSeatViewer.jsx`** — 4×2 seat grid tile: shows angel names +
  brain holders per lane, Mongo→cache refresh freshness, `Reseed
  canonical pairings` + `Force cache refresh` buttons. Reads
  `/api/admin/trader/status.state`, writes `/seed-seats` +
  `/reload-caches`.
- Both wired into `Overview.jsx` in a new `overview-trader-strip`
  row above the live regime strip.

### ✅ P2 Security Fixes (2026-07-01)

- **`eval()` in `/app/backend/ml/open_mythos/main.py:164`** — verified
  as PyTorch `nn.Module.eval()` (switch to eval mode), NOT Python
  builtin. Not a security issue. No change.
- **`random.Random(seed)` in `shared/seed.py` + `routes/doctrine_training_export.py`**
  — verified as intentional **deterministic** seeded sampling for
  reproducible demo data / stable train-eval splits. `# noqa: S311`
  already documents intent. Switching to `secrets` would break
  determinism. No change.
- **React `key={idx}` warnings** — fixed 5 files with fully **content-based** stable keys (no `idx`), per operator directive "if a dependency can stop the trade, remove it from the trade path" — same philosophy applied here: if index position can change the identity, use content, not index. Files: `DoctrineReference.jsx`, `IntentPostMortemPanel.jsx:1510`, `PipelineBlockerChip.jsx:239,262`, `ParadoxV3RolloutTile.jsx:439`, `FunnelDeltasTile.jsx:232`. Also proactively removed `idx` from the new `TradeTape.jsx` row loop.



Operator directive: "No database before broker submit. Local receipt
first. Small transactional DB second. Mongo third."

**Storage layers (in strict priority order):**
1. **Hot path** — in-memory dicts + `/app/trader/data/*.jsonl` (append-only, fsync per row)
2. **Truth tape** — `/app/trader/data/executions.sqlite` (WAL mode)
3. **Dashboard/archive** — Mongo (best-effort mirror via bounded queue; drops on timeout)

**New modules:**
- `/app/trader/store.py` — JSONL + SQLite + Mongo mirror worker
- `/app/trader/state.py` — in-memory seat/flag cache with 60s background Mongo refresher
- Rewritten: `seat.py`, `risk.py`, `audit.py` (all Mongo-free on hot path)

**New MC endpoints:**
- `GET  /api/admin/trader/health` — local store row counts + Mongo mirror lag
- `POST /api/admin/trader/reload-caches` — force out-of-band Mongo→cache refresh
- `GET  /api/admin/trader/{status,receipts,executions}` — now read from local SQLite (Atlas down = still works)

**Guarantee:** When Atlas is unreachable, the trader still trades. Broker
submits happen on cached seat/flag values, receipts land in JSONL + SQLite
synchronously, and Mongo catches up when it recovers. MC's dashboard
tiles keep serving from local SQLite.

**Tests:** `/app/backend/tests/test_trader_{store,state,risk}.py` — 23 passing.


### ✅ Pass 2 — Bulk Delete COMPLETED (2026-07-01)

Operator directive: "Pass 2 is a done deal. It's not trading either way, might as well delete the 11k."

**105 files deleted.** Every module that no longer had a place in the
Brain → Seat → Risk → Broker doctrine is gone.

Deleted shared modules:
```
shared/legacy_brain_wrappers.py       (CAMARO/REDEYE/CHEVELLE/ALPHA wraps)
shared/execution.py                   (dry_run simulator + auto_submit chain)
shared/auto_submit_policy.py
shared/auto_submit_receipt.py
shared/council.py
shared/consensus.py + consensus_engine.py
shared/direct_execute.py
shared/sovereign_mode_guard.py
shared/governor_policy.py
shared/market_regime.py
shared/brain_identity_migration.py
shared/seat_state.py
shared/advisor_opinions.py
shared/brains/camaro_weights_adapter.py
shared/brains/camino_committee.py
shared/brains/alpha_engine.py
shared/pipeline/       (whole folder — adapter, execution_pipeline, consensus_*, seat_policy, trigger_watcher)
shared/paradox_v2/     (whole folder — seed, verifier_loop, vote_doctrine_repo, vote_session_sweeper)
```

Deleted admin routes:
```
routes/admin_auto_submit.py
routes/admin_wrappers.py
routes/direct_execute_admin.py
routes/paradox_v2.py
routes/admin_paradox_v3.py
routes/seat_state_diagnose.py
routes/admin_seat_stage_drops.py
routes/admin_intents_post_mortem.py
routes/admin_intents_funnel.py
routes/admin_lane_readiness.py
routes/equity_trade_readiness.py
routes/intent_inspect.py
routes/intent_why.py
routes/unblock_report.py
```

Deleted tests: ~60 test files that only tested the deleted modules
(test_camaro_*, test_auto_submit_*, test_consensus_*, test_paradox_*,
test_legacy_wrapper*, test_direct_execute_*, test_council_*,
test_seat_state*, test_sovereign_*, test_dry_run*, test_unified_pipeline*,
test_authority_*, test_camino_committee*, test_governor*, test_roadguard*,
etc.)

**Not deleted (surprising retention)**:
- The 8-file trader (never touched — it doesn't use any legacy)
- 4 brain strategies (camino/barracuda/hellcat/gto — actively used by trader)
- MC's core UI backend (auth, healthcheck, snapshots, market data)
- The `executor_seat.py` fallback path (kept as legacy roster reader
  since the trader's `seat.get_holder` falls through to it)

**Backend verified booting clean** after deletes. `/api/admin/trader/*`
endpoints still respond (401 = auth required, correct). Trader end-to-end
cycle passes: Brain → Seat → Risk → Broker → executions.

**Residual (post-deploy)**:
- Some legacy admin tiles in MC UI will show empty or 404 (routes deleted).
  The trader's new tiles (`/api/admin/trader/{status,receipts,executions}`)
  are the source of truth.
- A handful of lazy imports in still-existing files (`shared/auto_router.py`,
  `shared/roster.py`, `routes/healthcheck_full.py`,
  `server_modules/lifespan.py`) reference deleted modules inside try/except
  blocks. They log warnings but don't crash. These get cleaned in a
  future pass.

## 2026-06-30 Operator Doctrine Pin — Path 2: MC = eyes, Trader = authority

After the prod 500s + persistent auto_router_loop hang, the operator
elected **Path 2** from the architectural triage:

```
MC (eyes only)                    Trader (authority)
─────────────                     ────────────────────────
AUTO_ROUTER_ENABLED=false         /app/trader/ — sidecar
BROKER_DISABLED=true              Market Data → Brain → Risk cap
auto_router cannot tick           → Broker → executions + receipts
broker_router refuses calls       runs in same FastAPI process
                                  same Mongo, same env vars
↓                                 ↓
reads `executions`,               writes `executions` (source=trader)
`trader_receipts` for display     writes `trader_receipts` per cycle
no trade authority                fires real orders
```

### What's live (2026-06-30, verified in preview)
- **`/app/trader/`** — 8 files, ~700 lines total:
  - `__init__.py`        — module marker + doctrine pin
  - `config.py`          — env-only configuration, no hardcoded values
  - `brains.py`          — 4 personality strategies (trend/mean-rev/breakout/momentum)
  - `feeds.py`           — Kraken OHLC + Yahoo equity, async httpx, computes RSI/SMA/MACD
  - `risk.py`            — per-order cap + daily cap + freeze + lane toggle + idempotency
  - `seat.py`            — reads `seat_registry` + legacy roster + DEFAULT_SEATS fallback
  - `broker.py`          — Kraken & Webull executors, ONE call per attempt
  - `audit.py`           — writes `executions` (source=trader) + `trader_receipts`
  - `main.py`            — async loop, bounded timeouts on every external call

- **MC neutralization** when `BROKER_DISABLED=true`:
  `shared/broker_router.py::route_order` raises `BrokerRouteBlocked` immediately
  with reason `broker_disabled_env_flag`. MC can never authorize a trade.

- **Sidecar startup** from MC's lifespan:
  When `TRADER_ENABLED=true` is set, `server_modules/lifespan.py` spawns the
  trader as a background asyncio task. Same process, same env, same Mongo. No
  supervisor changes needed (Emergent's `supervisord.conf` is read-only).

### Verified in preview
```
trader_receipts count: 2
  - 2026-06-30T18:29:11Z crypto XBTUSD  price=$58429.60 signals=4 chosen=HOLD
  - 2026-06-30T18:29:10Z equity TSLA    price=$418.80   signals=4 chosen=HOLD
```
Live Yahoo + Kraken data pulled in <500ms. All 4 brains ran. Seat doctrine
applied. No trades fired (correct — both verdicts HOLD).

### Required env vars on prod to activate
```
TRADER_ENABLED=true
AUTO_ROUTER_ENABLED=false
BROKER_DISABLED=true
TRADER_INTERVAL_SEC=60            # default
TRADER_PER_ORDER_USD_CAP=10       # default
TRADER_DAILY_USD_CAP=1000         # default
TRADER_CRYPTO_PAIR=XBTUSD         # default
TRADER_EQUITY_TICKER=TSLA         # default
TRADER_CONFIDENCE_THRESHOLD=0.55  # default
```
Broker keys (already in prod env per operator):
`KRAKEN_API_KEY`, `KRAKEN_API_SECRET`, `WEBULL_APP_KEY`,
`WEBULL_APP_SECRET`, `WEBULL_ACCOUNT_ID`.

### Operator endpoints (2026-06-30)
- `GET  /api/admin/trader/status` — task liveness, last cycle ts,
  fires today, spent today, env config
- `GET  /api/admin/trader/receipts?limit=50&lane=equity&fired_only=true`
  — per-cycle tape (signals + chosen + risk + broker_result)
- `GET  /api/admin/trader/executions?limit=50&lane=equity&ok=true`
  — only `source=trader` execution rows (broker truth tape)
- `POST /api/admin/trader/seed-seats` — idempotent. Writes the
  operator-canonical angel→brain pairings to `seat_registry`.
  Safe to call repeatedly. Run once after deploy:
  ```
  curl -X POST -H "Authorization: Bearer <JWT>" \
    https://mission.risedual.ai/api/admin/trader/seed-seats
  ```

### Canonical seat assignments (2026-06-30)
| Lane | Angel | Role | Brain |
|---|---|---|---|
| equity | Raziel  | strategist | camino    (trend) |
| equity | Nuriel  | governor   | hellcat   (breakout) |
| equity | Paschar | executor   | gto       (momentum) |
| equity | Sariel  | auditor    | barracuda (mean rev) |
| crypto | Remiel  | strategist | hellcat   (breakout) |
| crypto | Cassiel | governor   | camino    (trend) |
| crypto | Israfel | executor   | gto       (momentum) |
| crypto | Zadkiel | auditor    | barracuda (mean rev) |

Strategist+Executor are directionally compatible (both lean BUY on
real trends/breakouts → strict agreement produces trades). Mean
reversion is Auditor-only (observability, no veto).

### Pass 2 deletion — DEFERRED
Per operator pin (2026-02-27, reaffirmed 2026-06-30): the ~11,000 lines of
disconnected MC pipeline (`legacy_brain_wrappers`, `council`, `consensus*`,
`auto_submit_policy`, `pipeline/`, `direct_execute`, `sovereign_mode_guard`,
`paradox_v2/`, dry_run, 7 seat-sprawl files) remain present.
**Deletion is gated on**: trader fires at least one successful trade in BOTH
lanes. Until then, no deletions. Rollback safety net intact.

## 2026-02-27 Operator Doctrine Pin — Architectural Reduction

**Problem**: Every new capability was added without retiring an old
one. Result: ~11,000 lines of duplicate authority on the critical
path. Brains emit, but trades never fire.

**Mandate from operator**: "Reduce RISEDUAL to the smallest architecture
that still expresses my philosophy. One responsibility per layer."

### The 5-Layer Doctrine
```
Market Data → Brain → Seat → Risk → Broker
```

**Seat carries the FUNCTION. Brain keeps its PERSONALITY.**

Each lane has FOUR seats, each a distinct function. Brains rotate
into seats — Camino isn't "the trend brain forever"; Camino currently
holds (e.g.) the equity executor seat. Tomorrow a different brain
may hold it.

  | Seat function | Role in the decision |
  |---|---|
  | `strategist` | proposes the trade (its brain emits BUY/SELL) |
  | `governor` | sets the lane's risk regime (size multiplier 0.0–2.0) |
  | `executor` | authorizes routing to the broker |
  | `auditor` | recorded on the executions row for post-pass review |

**ONE PASS per complete decision.** `Seat.decide(intent)` returns a
single `SeatDecision` with all 4 holders + the governor's risk
multiplier already read. The caller multiplies notional once, calls
Risk, calls Broker, writes one `executions` row. No callbacks. No
"auditor objects" recheck loops. No council vote. No consensus pool.

Brain personalities (immutable, baked into `shared/brains/<name>/strategy.py`):
  * camino    — trend continuation (SMA/RSI/EMA filters)
  * barracuda — mean reversion (RSI/BB-position/trend)
  * hellcat   — breakout (BB-position/RSI/SMA20)
  * gto       — momentum (MACD/RSI/EMA cross)

### Delete / Keep (operator-directed, 2026-02-27)

**KEEP (new collections):**
- `seat_registry` — single source of seat assignments
- `brain_registry` — single source of brain tunables
- `executions` — single audit row per broker attempt
- `positions` (existing) — open position tracking
- `pnl_log` (existing) — realized P&L

**DELETE (collections):**
- `auto_submit_tiers`
- `vote_escalations`
- `governor_interventions`
- `roadguard_stops`
- `seat_promotion_log`
- `instrument_onboarding`

**DELETE (logic):**
- `SETUP: 2 CHECKS FAILED` blocking
- `DRY_RUN_PASSED/DRY_RUN_BLOCKED` gate
- `doctrine_reject` in execution path
- `auditor_objections` blocking
- `confidence_floor` below seat policy
- Wrapper double-execution
- Synchronous MC Memory query
- Synchronous LLM Ledger write
- Scorecard updates per intent
- Similar Past Setups query

## Implementation Status (2026-02-27)

### ✅ Completed — Architectural Reduction Pass 1
- **New modules built** (alongside legacy, additive):
  - `/app/backend/shared/seat.py` (~180 lines) — single Seat module.
    Merges 8 old seat files. `Seat.decide(intent)` returns
    `fire` or `pass`, period. Reads from `seat_registry` with
    legacy `shared_brain_roster` fallback for live operator
    assignments.
  - `/app/backend/shared/risk/check.py` (~150 lines) — single Risk
    pre-trade gate. Merges 6 cap/freeze/policy files. Returns
    `RiskCheck` with hard limits (freeze, lane toggle, per-order
    cap, daily exposure, idempotency).
  - `/app/backend/shared/executions.py` (~140 lines) — single audit
    collection writer. One row per broker attempt with broker
    response, exception, decision trail.
  - `/app/backend/shared/brain_registry.py` (~140 lines) — brain
    tunable + enabled state. Seeds defaults on first read.

- **auto_router rewired**: `_route_one` now uses
  Brain → Seat → Risk → Broker → executions directly. No more
  unified pipeline, no more dry_run, no more auto_submit_policy.
  `_tick` lost the seat-mismatch sweep (replaced by inline
  `Seat.decide` eligibility check).

- **Post-ingest chain neutralized**: `_fire_and_forget_dry_run` in
  `intents.py` now kicks `auto_router.force_one_tick()` instead of
  running dry_run + auto_submit_policy + council. The legacy
  `_run_dry_run_then_auto_submit` is a no-op for backward compat.

- **MongoDB pool config fixed** (`db.py`): proper `retryWrites`,
  `maxPoolSize=50`, `minPoolSize=5`, `maxIdleTimeMS=45s`,
  `serverSelectionTimeoutMS=15s`, `waitQueueTimeoutMS=10s`,
  `connectTimeoutMS=20s`. Fixes the "connection pool paused"
  Atlas symptom that killed the Kraken loop.

- **New indexes** (`db.py`): `executions_ts_desc_idx`,
  `executions_intent_idx`, `executions_lane_ts_idx`,
  `executions_ok_ts_idx`.

- **E2E verified**: smoke test confirms the autonomous auto_router
  routes real brain intents through the new path. Synthetic BUY
  intent flows Brain → Seat (vacant → pass) → Risk (ok) →
  executions row written.

### ⏳ Pending — Architectural Reduction Pass 2 (bulk delete)

**Operator timeline (2026-02-27 pin)**:
  * **This week**: legacy pipeline runs disconnected. New path is
    authoritative. Operator evaluates the old layers — anything
    actually doing work surfaces in this window.
  * **After both lanes (Webull equity + Kraken crypto) complete one
    successful end-to-end trade through the new path**: Pass 2 bulk
    delete proceeds.
  * **Until then**: do NOT delete any of the modules below. They
    remain present, importable, and unreferenced from the hot path.

The following files are NO LONGER in the hot path but still present
because 40+ admin routes/tests import them. They get deleted in a
follow-up commit once trades are verified flowing in prod:
- `shared/legacy_brain_wrappers.py` (1,204 lines)
- `shared/council.py` (1,102 lines)
- `shared/consensus.py`, `shared/consensus_engine.py`
- `shared/auto_submit_policy.py` (992 lines)
- `shared/auto_submit_receipt.py`
- `shared/direct_execute.py` (replaced by inline auto_router path)
- `shared/pipeline/` folder (execution_pipeline, adapter, governor,
  roadguard, consensus_*, seat_policy)
- `shared/execution.py` dry_run portion (~1,200 lines)
- `shared/sovereign_mode_guard.py` (663 lines)
- `shared/paradox_v2/` folder
- Multiple seat sprawl: `shared/auditor_seat.py`, `brain_seats.py`,
  `seat_policy.py`, `seat_state.py`, `seat_nudges.py`,
  `seat_performance.py`

### Admin route rebuild backlog
Tiles that currently read from the deleted layers will show empty
once Pass 2 ships. New tiles needed:
- "Recent Executions" — reads `executions` (replaces direct-execute-recent)
- "Seat Roster" — reads `seat_registry` (replaces 4 different seat tiles)
- "Brain Registry" — reads `brain_registry` (replaces wrappers / doctrine tiles)
- "Daily Spend" — aggregates `executions` (replaces exposure caps tile)

## Critical Path (current)
```
Brain emits intent
       ↓
shared_intents row inserted
       ↓
_fire_and_forget_dry_run → force_one_tick (≤50ms)
       ↓
auto_router._tick()
       ↓
_route_one:
  Seat.decide(intent)  →  if pass → executions row + stamp gate_state, done.
        ↓ (fire)
  Risk.check(intent)   →  if !ok → executions row + stamp gate_state, done.
        ↓ (ok)
  broker_router.route_order(...)
        ↓
  shared_intents.executed = True, gate_state = "submitted"
  executions row written with broker response
```

## Operator Verification Steps (prod)
1. Verify MongoDB pool no longer pauses:
   `curl /api/admin/healthcheck/full | jq '.checks.mongodb'`
2. Verify auto-router is ticking:
   `curl /api/admin/auto-router/status | jq '.task_alive,.last_tick_ts,.tick_count'`
3. Verify seat is assigned (must be present, or trades pass-out):
   `curl /api/admin/seat/list` (new) or check `shared_brain_roster`
4. Watch new executions roll in (replaces direct-execute-recent):
   `db.executions.find().sort({ts:-1}).limit(10)` from Mongo Atlas

## Test Credentials
See `/app/memory/test_credentials.md`.

## Backlog (P1/P2)
- Pass 2 bulk-delete commit (~11,000 lines)
- New admin tiles for `executions`, `seat_registry`, `brain_registry`
- POST endpoints for `seat_registry` operator assignment
- Webull paper-trading sandbox flow for testing fills before prod
- Resolve `auto_submit_policy ↔ execution` circular import (cleaned by
  the deletion in Pass 2)
- `IntentPostMortemPanel.jsx` refactor (1400+ lines)
- Train OpenMythos on RISE JSONL
- Hot-Brain Router into active pipeline
