# RISEDUAL Mission Control — PRD

## Original Problem Statement
Connect separate AI project runtimes (Barracuda, GTO, Camino, Hellcat)
into one monorepo-style Mission Control backend. Enable real-money
trading pilot with Webull (equity) and Kraken Pro (crypto). 5-stage
pipeline execution, doctrine-aligned vocabulary, strict cash-account
trading, comprehensive provenance + health tracking.

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
