# Hot-Path Atlas Audit — Synchronous Reads in the Live Execution Loop
Date: 2026-07-23 · Doctrine: "No live execution decision should depend on a synchronous Atlas read."

Scoring: CAPITAL = risk to open positions/money if Atlas is slow/down ·
FREQ = calls per unit · DIFF = migration difficulty

## P0 — capital protection

### 1. Exit policy read fails closed to DISABLED  ⚠ WORST FINDING — FIXED 2026-07-23
- Where: `shared/exits/policy.py::get_policy` (called every Exit Monitor tick, 20s)
- Behavior found: Mongo exception → DEFAULTS → `enabled=False` for both lanes
  → **an Atlas outage silently turns off stop-loss/take-profit enforcement**
  while positions stay open.
- CAPITAL: highest · FREQ: 1 read / 20s · DIFF: trivial
- Fix applied: last-known-good in-memory policy cache; Atlas failure returns
  the last successfully loaded policy (stamped `_stale=true`), DEFAULTS only
  before first successful load.
- Full target (package 1/3): policy snapshot in SQLite, async refresh.

### 2. Exit plans stored only in Atlas
- Where: `shared/exits/monitor.py` — `shared_exit_plans` reads/writes in
  `_reconcile`, `_reserve` (find_one_and_update), `_submit_exit`, `_adopt`
- CAPITAL: high (trigger evaluation + reservation depend on Atlas) ·
  FREQ: ~4-8 ops / tick · DIFF: medium (atomic reserve must move to SQLite)
- Target: plans in memory, SQLite transaction commits, Atlas mirror via
  outbox; restart rebuilds from SQLite + broker reconcile. (= operator
  work package "exit-plan migration", next in order)

## P1 — per-intent gate reads (latency + throughput)

### 3. Risk gate: 5–6 Atlas round trips per routed intent
- Where: `shared/risk/check.py::check` —
  `_daily_cap_effective` (runtime_flags) + `_is_freeze_on` (runtime_flags) +
  `_is_lane_enabled` (runtime_flags) + `_daily_spent_usd` (runtime_flags +
  `executions` aggregate, maxTimeMS 4000) + intent idempotency find_one
- CAPITAL: medium (fail modes block trades, don't lose money) ·
  FREQ: ×N intents / tick · DIFF: medium
- Target: `ExecutionPolicySnapshot` (versioned, atomic swap, async refresh)
  for caps/freeze/lane; daily-spent as in-memory counter incremented at
  execution time + SQLite persisted, rebuilt at boot (no per-intent aggregate)

### 4. Broker freeze state
- Where: `shared/broker_freeze.py` (find_one ×2) — same snapshot treatment.

### 5. Capital ledger reserve
- Where: `shared/capital/ledger.py` — `find_one_and_update` conditional
  reserve (atomic primitive on Atlas)
- CAPITAL: medium-high (correctness primitive) · DIFF: medium — single-process
  deployment means a SQLite transaction provides the same atomicity locally;
  Atlas mirror via outbox.

## P2 — selection + observability writes

### 6. Router pick query
- Where: `shared/auto_router_supervisor.py` (~line 201) —
  `shared_intents.find(...)` sort ingest_ts, max_time_ms 8000, 12s outer timeout
- FREQ: 1 / 30s tick · DIFF: high (touches intent ingestion + sweeper)
- Target: local durable intent queue (MC Pulse → SQLite queue + memory
  index → router); Atlas keeps intent history async. (= package 4)

### 7. Intent gate_state stamping: ~15+ `shared_intents.update_one` per intent
- Where: `shared/auto_router_stages.py` (19 call sites)
- CAPITAL: low (audit trail) · FREQ: very high · DIFF: low-medium
- Target: route through the outbox (`intent_stamp` events) or batch per tick.

### 8. `executions.record` inserts (route audit rows) — outbox candidates.

## Already mitigated (no action)
- `trading_controls` arm gate: 2s TTL cache + 300s last-known-good grace
  (`auto_router.py` 2026-07-22)
- Conviction floor: TTL cache (`auto_router_stages.get_conviction_floor`)
- Opportunity policy: TTL cache (`shared/opportunity/policy.py`)
- Exit outcomes + permanent receipts: durable SQLite outbox (2026-07-23) ✓
- Brain decision inputs: indicator snapshots read per tick but brains
  fail-soft to HOLD (no capital at risk on read failure)
- Adoption enrichment reads (`_brain_levels`, `_origin_intent`,
  `_entry_price_fallback`): fail-soft to lane defaults — acceptable.

## Recommended migration order (matches operator packages)
1. DONE — durable outbox (outcomes + receipts)
2. DONE — exit policy last-known-good cache (P0 #1)
3. NEXT — exit-plan migration to memory+SQLite (P0 #2)
4. ExecutionPolicySnapshot + local daily-spent counter (P1 #3, #4)
5. Capital ledger → SQLite atomic reserve + Atlas mirror (P1 #5)
6. Local router intent queue (P2 #6)
7. Intent stamping via outbox (P2 #7, #8)
