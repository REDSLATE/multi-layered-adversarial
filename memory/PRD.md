# RISEDUAL Mission Control — PRD

## Original Problem Statement
Connect separate AI project runtimes (Barracuda, GTO, Camino, Hellcat)
into one monorepo-style Mission Control backend. Enable real-money
trading pilot with Webull (equity) and Kraken Pro (crypto). 5-stage
pipeline execution, doctrine-aligned vocabulary, strict cash-account
trading, comprehensive provenance + health tracking.

### 🚨 NEXT WORK ITEM — P0 UNSTARTED (top priority — do NOT skip past this)


**🚧 2026-07-11 (iter-26): MC SEAT ARBITER + DAWE — ARCHITECTURE FROZEN, IMPLEMENTATION UNSTARTED.**

**Operator context (verbatim doctrine — do not paraphrase, do not soften)**:

The system was diagnosed as **too timid**. Operator listed 8 timidity levers to remove:

1. Fixed RVOL floor
2. Fixed confidence floor
3. Mandatory multi-brain agreement
4. Mandatory setup quality grade
5. Hard doctrine vetoes
6. Static spread penalties when quotes are stale
7. Single-expression-only logic
8. Paper/shadow-only routing

Operator's stated design correction: **"Use history as a weak prior, then let current-day evidence dominate quickly."** History remains present but has much less authority than current conditions. This is codified as the **Day-Adaptive Weight Engine (DAWE)**.

**Operator's second correction (equally important)**: MC must become MORE than a hub. **MC becomes the seat arbiter, and DAWE weights are installed AT THE SEAT with all brains as members of MC** — not per-brain-in-isolation. This is because the seat (a specific symbol × lane × entry window) is where brains actually compete. Weights that live per-brain globally aren't commensurable.

**Operator's third correction**: **No more shadow anything applied to the active pipeline.** Paper trading, "would-have-executed" simulators, and shadow arbiters running alongside live are all waste. **Learning surfaces that grade real predictions against real market movement (Kernel Review, counterfactual_signals, RISE AI, memory_kernel) STAY** — those aren't shadow, they're evaluation of reality. What must be killed on sight: any parallel arm of the active brain → arbiter → trader → broker pipeline.

**Runtime modes reduced to two**: `DISARMED` (opinions collected, arbitration runs, DAWE grades update, NO intent to trader) and `LIVE` (intent IS emitted, kill switch + mechanical validators still gate). **No PAPER. No SHADOW. No "would-have-picked" side channel.**

**Decisions locked in this session**:
- **Repo**: stay in this repo (`/app/backend/mc_arbiter/`). Not a greenfield rewrite. Reuse Webull/Kraken adapters, Polygon/Finnhub/Kraken feeders, Atlas plumbing, dashboard shell, auth, tests. Rip out brain-side gates + duplicate sizing math instead.
- **Seat key**: `f"{lane}:{symbol}:{5min_bucket_iso}"` — bounded cardinality (~288 seats/symbol/day), matches 15m grade horizon, gives brains ~5 min to submit before arbitration.
- **DAWE state scope**: **(brain, lane)** at v0.1. NOT (brain, symbol). Escalate to (brain, symbol) only if per-symbol dispersion proves meaningful.
- **DAWE weight math**: `effective = clamp(session^0.50 × recent^0.30 × prior^0.20, 0.40, 1.40)`. Session EWMA α=0.30 over last 90 min. Recent EWMA α=0.10 daily. Prior static at 1.0 for cold start; nightly cold-refresh added in Phase 2. Cold-start guard: if `grades_used_session < 5`, return `effective=1.0` (thin data can't move the arm).
- **Rank vs size split**: rank uses `× effective`, sizing uses `× sqrt(effective)` — softens size damage from a temporary weak period. Final size gate: `clamp(total_size_multiplier, 0.30, 2.00)`.
- **SessionContext primitives** (trend_strength, breadth, correlation, news_intensity) — **DEFERRED to Phase 2**. v0.1 uses graded-outcome signal only. Rationale: those primitives are non-trivial to compute honestly on a small universe; ship the honest signal first, add primitives if the arm proves under-adaptive.
- **Regime-change detector with hysteresis** — DEFERRED to Phase 2. Static α good enough at v0.1.
- **Historical prior warmup** — DEFERRED to Phase 2.

**Data locations (agreed)**:
- New collection `mc_seats` (compound index `(seat_key, brain)`, TTL 30 days) — holds opinions + winner + receipt per seat.
- DAWE state extends `brain_runtime_metrics.risedual_stack.brains.<brain>.dawe.<lane> = {session, recent, prior, effective, grades_used_session, grades_used_recent, last_updated}`. **One doc read gives the whole matrix.**
- Grader progress on each opinion doc; no separate collection.

**What gets DELETED after arbiter is armed (Bruce Lee dividend)**:
- Brain-side confidence floors (per-brain `MIN_CONFIDENCE`)
- Brain-side RVOL gates
- Brain-side sizing math (all duplicates)
- Brain-side direct calls to `shared_intents.insert_one` (route through MC now)
- Any "would-have-executed" simulator or parallel shadow arbiter

**What is KEPT**:
- Kernel Review, RISE AI, memory_kernel, counterfactual_signals (learning surfaces on the REAL tape — NOT shadow)
- Kill switch, master switch, mechanical validators (physical valve)
- 3-clock write health, Atlas timeout handler (infra from iter-25)

**Full design freeze is at `/app/memory/MC_SEAT_ARBITER.md`. NEXT AGENT MUST READ THAT DOC BEFORE TOUCHING ARBITER CODE.** The freeze defines: `ModelOpinion` shape, `SeatKey` format, `DaweState` schema, arbitration loop steps 1-7, grader cadence, deletion list, sign-off checkboxes.

**Implementation state as of this handoff**:
- ✅ `/app/memory/MC_SEAT_ARBITER.md` — 12-section design freeze written, all 6 §12 sign-off checkboxes marked done by operator on 2026-07-11.
- ✅ `/app/backend/mc_arbiter/__init__.py` — module docstring + design pointer.
- ✅ `mc_arbiter/models.py` — ModelOpinion (frozen), DaweState, SeatDoc, Direction, RuntimeMode. `rank_score = edge × confidence × regime_fit × (0.75 + urgency × 0.25)` matches v3 exactly. RuntimeMode has ONLY DISARMED and LIVE — enum-completeness test guards against a third mode being added silently.
- ✅ `mc_arbiter/seat_key.py` — `bucket_iso` (5-min UTC floor), `build_seat_key` (`lane:SYMBOL:iso`), `parse_seat_key` (handles `ETH/USD`-style slash symbols), `next_bucket_iso`. Naive datetimes treated as UTC (never local — anti-3-clock-regression).
- ✅ `mc_arbiter/dawe.py` — `ewma`, `compute_effective_weight` (cold-start guard at 5 grades, geometric mean of session^0.50 × recent^0.30 × prior^0.20, clamped [0.40, 1.40]), `size_multiplier` (sqrt softening), `update_session` (α=0.30), `update_recent` (α=0.10), `quality_from_signed_return`.
- ✅ `mc_arbiter/arbiter.py` — `load_dawe`/`save_dawe` (persistence to `brain_runtime_metrics.risedual_stack.brains.<brain>.dawe.<lane>`), `submit_opinion` (upsert by (seat_key, brain), idempotent), `arbitrate` (full 7-step loop: collect → DAWE-adjusted rank → argmax among directional → disagreement from opposition → size = kernel × disagreement × √effective clamped [0.30, 2.00] → emit via `shared.intents._post_intent_impl` only if LIVE → record on the winner's row), `get_runtime_mode`/`set_runtime_mode` (default DISARMED with attributable audit trail).
- ✅ `mc_arbiter/grader.py` — `grade_pending_opinions` (idempotent per-horizon), `_grade_one` (LONG/SHORT/FLAT with FLAT symmetric grading), `_price_at` (bounded read of `shared_ohlcv_bars` 1m tf, "at or before" horizon → NO lookahead bias), `roll_recent_end_of_day` (α=0.10 daily fold + 30%-toward-neutral session reset). Manual endpoint only in v0.1 — background scheduler wiring is Phase 2.
- ✅ `mc_arbiter/routes.py` — `POST /api/mc/arbiter/opinion`, `POST /api/mc/arbiter/arbitrate/{seat_key}`, `GET /api/mc/arbiter/seat/{seat_key}`, `GET /api/mc/arbiter/state`, `POST /api/mc/arbiter/runtime-mode`, `POST /api/mc/arbiter/grader/run`. All admin-authed. Pydantic validators reject unknown lane/direction/mode at the boundary.
- ✅ `mc_arbiter/tests/` — 65 tests / 0.17s / 100% pass. Unit: rank_score arithmetic, enum guards, DaweState roundtrip, EWMA convergence, bound clamps, cold-start, quality mapping, seat_key roundtrip incl. slash symbols, timezone honesty. Integration (real Mongo): 4-brain competition, DAWE weight flipping the winner, cold-start neutrality, all-FLAT no-winner, empty-seat safety, disagreement multiplier shrinks size on split field, runtime mode default + flip round-trip.
- ✅ Router registered — `mc_arbiter_router` mounted after `intents_router` in `server_modules/router_registry.py`. Backend restart shows `/api/mc/arbiter/state` returns `{"runtime_mode":"DISARMED", ...}` on cold boot.
- ✅ Indexes added to `db.ensure_indexes`: `mc_seats_seat_brain` (unique compound), `mc_seats_brain_lane_ts`, `mc_seats_ts_grader`, `mc_seats_recorded_at`. TTL activation deferred one iteration until we switch `recorded_at` to BSON date at write time (currently ISO string — cheap fix but out of scope this pass).
- ✅ **End-to-end smoke verified via curl** (2026-07-11): Camino LONG + Barracuda SHORT posted → arbitrate returned Camino as winner (higher rank_score), disagreement_multiplier=0.902 (opposition_strength × 0.40 penalty), size_multiplier=0.902, `runtime_mode=DISARMED` + `intent_id=null` confirmed (no trader emission). Contract holds.

**Deletion pass NOT YET STARTED** (blocked on arbiter being armed + running in LIVE against actual brain-emitted opinions):
- ⏳ Brain-side confidence floors (per-brain `MIN_CONFIDENCE`)
- ⏳ Brain-side RVOL gates
- ⏳ Brain-side sizing math (all duplicates)
- ⏳ Wire the 4 in-process runners (`/app/external/brains/runner.py`) to POST `/api/mc/arbiter/opinion` alongside their current `shared_intents.insert_one` — dual-write for one iteration to confirm parity, then cut the direct write.
- ⏳ Background scheduler for the grader (60s cadence) inside `server_modules/lifespan.py`.

**Immediate next step for the agent picking this up**:
1. **DO NOT ARM (do not flip runtime_mode to LIVE)** until step 3 below is proven end-to-end.
2. Wire brain runners to also POST opinions to `/api/mc/arbiter/opinion` (dual-write pattern — brain still emits its own intent AND submits opinion to MC). Add a `SHARED_INTENTS_BYPASS_ARBITER=1` env flag as a temporary shim so we can toggle whether the brain's direct write survives.
3. Confirm all 4 brains show up in `mc_seats` for the same 5-min bucket when they're looking at the same symbol. `GET /api/mc/arbiter/seat/{seat_key}` should return 4 opinion rows.
4. Confirm the grader picks up 15m-aged opinions and stamps `grade_15m` — verify via `db.mc_seats.find({grade_15m: {$exists: true}})`.
5. Confirm DAWE state moves — read `brain_runtime_metrics.risedual_stack.brains.<brain>.dawe.<lane>` and see `session_weight` drift from 1.0 after ~10 graded predictions per (brain, lane).
6. THEN and only then: flip a single brain-lane to LIVE via `/api/mc/arbiter/runtime-mode`, watch for one full session, and iterate.

**Full design freeze at `/app/memory/MC_SEAT_ARBITER.md`. NEXT AGENT MUST READ THAT DOC BEFORE TOUCHING ARBITER CODE.**


**🚧 2026-07-11 (iter-27): MC PULSE — ARCHITECTURE FROZEN, IMPLEMENTATION UNSTARTED.**

**Operator directive (verbatim, do not paraphrase)**: "Are runners necessary? Couldn't the connection of being in one stack be enough for the brains?" Answered: YES, runners are not necessary. Runners were never architectural boundaries — they were duplicated orchestration wrapped around four strategy calls. Collapse them into a single MC pulse. Brains become Python objects with one method: `evaluate(snapshot) -> ModelOpinion | None`.

**Governing doctrine (locked in this session)**: "MC owns time, data, scheduling, arbitration, persistence, and execution routing. Brains own only interpretation." MC must NOT standardize interpretation — that destroys the four brains by turning them into `personality_multiplier[brain_id] * common_strategy(snapshot)`. Each brain keeps its own doctrine class, memory state, thresholds, feature selection, confidence formation, and objection vocabulary (`reason_codes`).

**Five operator corrections to my initial pulse sketch (all accepted)**:

1. **Immutable snapshot**: brains receive a `frozen`/`slots` `MarketSnapshot` with a `Mapping[str, float]` for indicators. Contamination between brains is a silent-bug factory — my initial mutable dict was wrong.
2. **Contain each brain independently**: `asyncio.wait_for` + per-brain try/except INSIDE `evaluate_brain`. `asyncio.gather(return_exceptions=False)` only raises on orchestration failure, never on brain failure. A broken Hellcat must NEVER silence Camino/Barracuda/GTO.
3. **Envelope split**: brains return opinions ONLY. MC wraps them in `OpinionEnvelope(pulse_id, brain_id, seat_key, snapshot_id, opinion, evaluated_at)` — brains never see `seat_key`, `pulse_id`, Mongo, or the trader.
4. **Idempotency via `pulse_id`**: every pulse has a unique id. Unique indexes on `(pulse_id, brain_id, symbol, lane)` for opinions and `(pulse_id, seat_key)` for arbitrations. A restarted pulse fills gaps but CANNOT create a second executable decision. My initial sketch had no retry-safety at all.
5. **Split pulse health from brain health**: `PulseReceipt` reports orchestration health AND per-brain evaluation status (`brains_expected`, `brains_completed`, `brains_failed[{brain, reason, exc_type}]`, `overrun`). "Pulse healthy" MUST require `brains_failed == []` — a green pulse hiding a dead brain is the exact 3-clock dishonesty we've been eliminating.

**Architectural correction accepted**: grader OFF the critical pulse path. Critical = snapshot → evaluate → persist → arbitrate → route. Non-critical maintenance = grade → rollups → cleanup → performance metrics. Same MC ownership, separate failure paths. My initial sketch called `grade_pending_opinions()` inline in `pulse_tick()` — wrong. Pulse *enqueues* due grades; a separate worker executes them.

**Migration order (8 steps — one brain at a time, comparison-only before switch)**:
1. Build pulse infra + registry + immutable `MarketSnapshot` + `OpinionEnvelope` + `PulseReceipt` + idempotency contracts (unique indexes).
2. Adapt ONE brain (simplest first — TBD) to `.evaluate(snapshot)`. Keep its runner running.
3. Comparison-only mode: pulse calls the adapted brain in parallel with its runner; opinions written to `mc_opinions_compare` (NOT `mc_seats`, NOT arbitrated). NO duplicate submission.
4. Confirm parity: action rate, confidence distribution, `reason_codes` overlap, timestamp behavior within acceptable drift.
5. Move remaining brains one at a time, repeating steps 2–4.
6. Switch arbitration input to pulse-owned `OpinionEnvelope`s (arbiter reads from pulse-populated `mc_seats`, not runner direct-write).
7. Delete runner scheduling + direct writes (only after every brain on pulse AND arbitration reads pulse envelopes).
8. Remove sidecar identity + heartbeat plumbing ONLY after grep confirms no reader depends on `sidecar_checkins` / `shared_heartbeats` / `bump_stack_heartbeat` callers.

**Do NOT attempt a big-bang rewrite. Every step must be individually revertable.**

**Personality preservation acceptance tests (MUST be in place before step 6)** — parity of outputs is NECESSARY BUT INSUFFICIENT. Tests must also prove the four brains stay four distinct minds:
- `test_camino_and_barracuda_have_distinct_reason_codes` — reason_codes must differ
- `test_action_distribution_stays_diverse` — fraction of ticks where all 4 chose the same action stays below threshold (`distinct_action_rate >= 0.35`)
- `test_pairwise_confidence_correlation_bounded` — `pairwise_confidence_correlation(a, b) < 0.85` for every pair (two brains that always agree are one brain in two skins)
- `test_brain_specific_features_are_used` — Camino must actually read its momentum features via the snapshot-access trace, Barracuda must actually read its mean-reversion features, etc.

If these fail during migration, the doctrine-collapse risk is real and the migration must halt.

**Implicit contracts hidden in runner code — MUST be captured explicitly in MC before ANY runner is deleted**:
- Normalization (symbol casing, tf naming, quote timestamp rounding)
- Freshness rejection (quotes / bars older than N seconds skipped)
- Cadence rules (some brains 30s, some 60s, some session-boundary only)
- Roster attribution (which brain assigned which seat/symbol)
- Runtime modes (DISARMED vs LIVE — already owned by arbiter)
- Evidence stamping (what goes into the intent's `evidence` field)
- Exception behavior (which errors halt the tick, which log-and-continue)
- Deduplication (same-tick same-symbol re-emission guards)

An audit checklist per runner MUST be produced before that runner is deleted.

**Data locations**:
- `mc_pulses` (new): `_id=pulse_id`, holds `PulseReceipt`, TTL 7 days.
- `mc_seats` (existing from arbiter Phase 1): new unique index `(pulse_id, brain_id, symbol, lane)`.
- `mc_arbitrations` (new): unique `(pulse_id, seat_key)`, stores decision snapshot + intent_id.
- `mc_brain_state.{brain_id}.{strategy_version}` (new pattern): each brain's namespaced rolling state. NEVER shared across brains.
- `brain_runtime_metrics.risedual_stack.pulse.*`: mirror of latest `PulseReceipt` for dashboard.
- `mc_opinions_compare` (temporary, deleted at migration step 7): parity comparison rows.

**Directory layout agreed**:
```
/app/backend/mc_pulse/
    pulse.py · snapshot.py · registry.py · envelope.py · receipt.py
    containment.py · protocols.py · tests/
/app/backend/mc_brains/
    camino.py · barracuda.py · hellcat.py · gto.py
```

**What survives from `mc_arbiter/` (iter-26)**: everything. Pulse is an additional layer, not a replacement.
- `models.py` (ModelOpinion, DaweState, Direction, RuntimeMode) — reused
- `seat_key.py`, `dawe.py` — reused
- `arbiter.py` — reused; `submit_opinion` becomes an internal call from `pulse.upsert_many`
- `grader.py` — moves to separate background worker (off critical pulse path)
- `routes.py` — kept for admin/debug injection + runtime-mode flip; brains no longer POST here

**Full design freeze at `/app/memory/MC_PULSE.md`. NEXT AGENT MUST READ BOTH `MC_SEAT_ARBITER.md` AND `MC_PULSE.md` BEFORE TOUCHING PULSE CODE.**

**Implementation state as of this handoff**:
- ✅ Design freeze `/app/memory/MC_PULSE.md` (15 sections)
- ⏳ Operator sign-off pending on §15 checkboxes (8 items). Two questions specifically open:
  - **Which brain is "simplest" for migration step 2?** (Operator to designate — likely Camino or GTO based on strategy complexity.)
  - **Brain protocol shape**: stateful class (`class CaminoBrain: async def evaluate(self, snapshot)`) or stateless functional (`(prev_state, snapshot) -> (new_state, opinion)`)? Current lean is class for v0.1, functional for Phase 2 if it matters.
- ⏳ `mc_pulse/*` — NOT YET WRITTEN. Do NOT start until §15 checkboxes are approved.
- ⏳ Runner audit checklists (per §8 of MC_PULSE.md) — NOT YET PRODUCED. Must exist before ANY runner deletion.


**🚧 2026-07-11 (iter-26): MC SEAT ARBITER + DAWE — IMPLEMENTATION COMPLETE (DISARMED, END-TO-END VERIFIED).**


**✅ 2026-07-11 (iter-25b): ATLAS TIMEOUT SYSTEM-WIDE SAFETY NET.** After the 3-clock work landed, prod still showed `NetworkTimeout: customer-apps-shard-XX.kndgvm.mongodb.net:27017` and `ExecutionTimeout: PlanExecutor error during aggregation :: operation exceeded time limit, MaxTimeMS...` red banners across the Overview page (Feeder Slots, Shared Technical Feed) and BrainConsole (Barracuda). Audited: 295 unbounded read sites — patching each individually is a losing game. Two-part fix: (a) explicit `.max_time_ms(2500)` / `maxTimeMS(4500)` bounds + fail-soft try/except returning `{items:[], degraded:true}` on `/shared/opinions` × 2 handlers, `/shared/technical/symbols`, `/shared/technical/feeders`; (b) GLOBAL FastAPI exception handler in `server_modules/middleware_setup.py` catching pymongo `NetworkTimeout`, `ExecutionTimeout`, `ServerSelectionTimeoutError`, `WTimeoutError` — for GET/HEAD returns HTTP 200 with `{ok:false, degraded:true, atlas_timeout:true, items:[], count:0, payload:{}, request_id, warning}` (a 200 is deliberate so every widget's happy path resolves and widgets render empty state instead of a red banner); for writes returns HTTP 503 with same body (writes stay honest — a POST that timed out MUST reach the caller so retry / user feedback fires; this is the anti-silent-swallow doctrine extended to the write path at the middleware layer). Handler registered BEFORE the generic Exception handler so FastAPI resolves the more-specific pymongo classes first. 3 new tests validate the response shape + registration completeness. Also frontend: `TraderSeatViewer.jsx`, `SpreadWatcher.jsx`, `TraderPostMortem.jsx` now hide the whole card on 404 (their backend endpoints were removed in the earlier simplification pass but widgets were still mounted showing "Not Found" red banners). 28/28 tests pass. **Long-term direction for the Atlas problem**: materialize hot dashboard state into single summary docs (the `brain_runtime_metrics.risedual_stack` pattern) — Trader Seats status, Feeders status, Technical universe summary — so the dashboard reads O(1) instead of aggregating live. That makes Atlas tier irrelevant for the operator UI. Only bump to M10 dedicated (~$57/mo) if load remains after materialization.


**✅ 2026-07-11 (iter-25): 3-CLOCK INTENT-WRITE HEALTH + PROD MONGO NETWORK-TIMEOUT HOTFIX SHIPPED.** Operator directive: "heartbeat fresh ≠ intent pipeline healthy." Split the brain-side truth from the DB-side truth with FOUR distinct stamps per brain in the shared `brain_runtime_metrics._id="risedual_stack"` doc (everything is on ONE stack): `last_heartbeat_ts` (runner alive), `last_decision_ts` (decision produced this tick, HOLD included), `last_db_confirmed_intent_ts` (Mongo `insert_one` for ANY action confirmed — HOLD still proves the writer is alive), `last_db_confirmed_directional_intent_ts` (BUY/SELL/SHORT/COVER only — separated so the UI can distinguish "writer healthy but no directional opportunity" from "writer dead"). Counters $inc'd monotonically: `decisions_total`, `intent_submit_attempts_total`, `intent_submit_successes_total`, `directional_submit_successes_total`, `intent_submit_failures_total`. Insert-failure receipt (`last_intent_submit_error_ts/_msg/_action/_symbol`). Anti-silent-swallow guarantee: `shared/intents.py` wraps the `shared_intents.insert_one` in try/except that bumps failure metrics (best-effort) then **RE-RAISES** — the runner MUST hear about the failure. Verified with a monkeypatch test that patches `AsyncIOMotorCollection.insert_one` at the class level to raise `RuntimeError("simulated_atlas_outage")`; the counter advances by 1 AND `_post_intent_impl` propagates the exception. Session-aware `write_health` band (HEALTHY / STALE / DEAD / UNKNOWN / BLIND) — during equity closed-market hours, >60m directional-write age relaxes to STALE (HOLD-only writes are legitimate steady state overnight); heartbeat stale → BLIND regardless. Endpoints: `GET /api/admin/runtime/stack/status` attaches `_ages` + `write_health` per brain and `equity_market_open` at top level; `GET /api/admin/runtime/{brain}/status` embeds a full `payload.write_health` block (`{band, ages, counters, last_write_receipt, last_error, equity_market_open}`). Wired `bump_stack_heartbeat` into `sidecar_checkin.py` and `ingest.py` so heartbeats stamp the stack clock (nobody was calling this — every brain was reporting BLIND). Frontend `BrainProxiedStatusTile.jsx` now renders a "Write health · 3 clocks" section with a colored band pill, three clock tiles (heartbeat / last decision / DB write (any)), a counters row (attempts / success / directional_ok / failures / decisions), a last-write-receipt block (intent_id, symbol, action, lane, ingest_ts), and a last-submit-error block. All 11 required `data-testid` hooks resolved. 6/6 new unit + integration tests pass; full regression suite green. **Prod NetworkTimeout hotfix (same iter)**: prod symptom "NetworkTimeout: customer-apps-shard-00-01.kndgvm.mongodb.net:27017: The read operation timed out" blanket-covered BrainConsole and other pages. Root cause: `/api/shared/opinions?runtime=X&limit=10` was doing `find({runtime:X}).sort(posted_at:-1)` against `shared_opinions` with **no compound index** — full collection scan on a growing tape. Fix (2 parts): (a) added `shared_opinions_runtime_posted_at`, `shared_opinions_topic_posted_at`, `shared_opinions_thread_posted_at`, `shared_opinions_posted_at` indexes via `_safe_create_index` in `db.ensure_indexes`; (b) bounded the query with `.max_time_ms(2500)` + try/except that returns `{items:[], count:0, degraded:True}` on any Motor error (never renders a red banner across the operator view). Applied to both `/shared/opinions` and `/runtime-discussion/opinions` variants. Also fixed the "Trader Post-Mortem — Not Found" 404 banner: `/admin/trader/receipts` was removed in the earlier simplification pass but `TraderPostMortem.jsx` was still mounted on the Intents page; it now hides the whole card on 404 instead of showing a red banner. **Login redirect fix**: `Login.jsx` was navigating to `/admin/hypothesis` (a deleted route), which the catch-all bounced to `/` (marketing splash) — operators had to refresh to reach the admin dashboard. Switched to `/admin/overview`.


**✅ 2026-02-19 (iter-25): SIMPLIFICATION PASS — Bruce Lee doctrine ("Remove what is not needed").** Operator halted P2/P3 expansion; RISEDUAL had become too layered. Ran a KEEP/DELETE/FREEZE audit and eliminated the DELETE bucket. **Reverted my own recent work**: Gate-Tuning queue (tuning_signals.py + endpoints + gate cache in doctrine_overlay + KernelReview.jsx dual-queue UI + test file + namespaces entry) — user's rule "no multiple overlapping tuning queues, no automatic threshold overlays" makes this exactly the wrong direction. **Backend deletions (~35 files)**: paradox suite (5 routes + 4 services + seed script), scorecard duplicates (scorecard_by_brain, learning_scoreboard), advisor_performance surfaces, admin_trader (sidecar dashboard), opinion_silence_watchdog, era_comparison, shadow_outcome_admin, data_council_admin, canary_admin, parabolic_phase_admin, heartbeat_reconciler_admin, kraken_manual_reconcile, orphan tools (inspection + replay), legacy trader-sidecar routes (verifier, sidecar_diagnostics, trader_broker_check, trader_warmup_admin), doctrine_training_export, doctrine_eval, research, hypothesis, promotion. Workers stopped: opinion_silence_worker, witness_resolver_runner, shadow_close_cron, heartbeat_reconciler, paradox_record. `server_modules/lifespan.py` and `router_registry.py` cleaned. **Frontend deletions (14 pages)**: Discussion, Witnesses, Redeye, PublicTraffic, FeatureBuilders, Setup, MemoryFirewall, Ping, SeatContext, Artifacts, Hypothesis, Promotion, Calibration, Scorecards. `App.js` rewritten. **Sidebar collapsed 6 → 4 groups**: Live (Overview/Positions/Intents/Receipts) · Learning (Kernel Review/Doctrine Ref) · Diagnostics (Flags/Diagnostics/Live Tail/MC Memory/LLM Ledger) · RISE_AI. **Kept per operator exception**: Kernel Review, Rise AI (routes + threads + shared + page), memory_kernel_routes + memory_labeler + memory_modulator. **Result**: 2848 tests passing / 1 known load-contention flake (pre-existing p95 sustained-load probe). Backend healthy, brains still emitting, all target nav items live.



**✅ 2026-02-19 (iter-24 · followup): P2 UI complete + P3 first-pass refactor SHIPPED.** **P2 UI**: `KernelReview.jsx` rewritten as a queue-parameterized page. Top-level `[Sizing Lessons | Gate Tuning]` queue switcher shares the state tabs + guardrail card + analyze/refresh controls + card list; each queue defines its own endpoint map, kind meta, guardrail copy, and evidence renderer. New `TuningEvidence` component surfaces missed-win % / wilson MW ↓ / shrunk avg / missed wins / correct blocks / undetermined / avg return. Analyze button swaps label ("Run analyzer" ↔ "Run tuner") and success toast counts (edge/bleed ↔ relax/preserve). Sort per queue: relax first / edge first, then by |shrunk|. All testids scoped by `${queue.key}-` prefix. Smoke-tested in preview — both queues load, empty states render, counters update on state-tab switch. **P3 first pass**: `shared/auto_router.py` shrunk 1761 → 1256 lines (**-505 lines, -29%**) by extracting reconciliation & expiration sweeps to new `shared/auto_router_reconciliation.py` (567 lines). New home for `_sweep_expired_unrouted`, `_sweep_submitted_broker_orders`, `_finish_sweep`, `_minutes_since_iso`, `RECONCILE_*` tunables, and `_LAST_RECONCILE_SWEEP_TS` module state. `auto_router.py` re-imports these names for backward compat — external callers unchanged. 3 test files updated to patch the extracted module. Full suite 3073 passed / 0 failed. **Deferred to future P3 iterations**: extract `_tick`/`_loop`/supervisor helpers (~240 lines), then break `_route_one` (~800 lines) via a route-context object.



**✅ 2026-02-19 (iter-24): Kernel Review Stage 3 UI + counterfactual upgrades + full-suite hardening SHIPPED.** Three landings in one pass. **(1) Stage 3 UI** (`/admin/kernel-review`): new `pages/KernelReview.jsx` — state-tabbed queue (proposed / approved / rejected / applied) with per-lesson evidence grid (samples, hit rate, Wilson lower, avg 5m/1h bps, shrunk EV, wins/losses) + Approve / Reject buttons + Run Analyzer button. Nav item added to Governance group. Every button/row has a unique `data-testid`. Doctrine banner on the proposed tab makes the guardrail floors visible to the operator. **(2) Counterfactual signal upgrades** (operator spec b + c, applied in-place at `shared/counterfactuals/` — same collection, zero data migration): distilled signals now carry `may_execute=False` / `broker_access=False` (belt-and-suspenders execution firewall) + `experience_type="counterfactual"` (bucket-analyzer dimension so counterfactuals never share a bucket with executed learning experiences); SHORT/COVER support already present, test coverage extended; two-stage `purge_state="distilling"` in `intent_sweeper.py` (stamp → distill → stamp-guarded delete; distill failure clears the stamp so next sweep retries); directional-blocked archive path removed — the counterfactual signal IS the durable record. **(3) Counterfactual → gate-tuning bridge** (P2 backend): new `shared/counterfactuals/tuning_signals.py` aggregating by `(blocked_reason, lane)` and emitting RELAX_GATE / PRESERVE_GATE proposals when ≥30 samples AND Wilson ≥ 0.60 AND |shrunk_avg| ≥ 5 bps; new `doctrine_overlay.get_gate_threshold_delta(blocked_reason, lane, db)` returning clamped ±0.20; new admin endpoints (`POST /counterfactuals/tune`, `GET/POST /counterfactuals/tuning-signals`); new `counterfactual_tuning_signals` collection. **Full-suite hardening**: 4 rotating flakes stabilized surgically (master-switch arm patch on 4 `_route_one` test scaffolds; p95<2s + hard-max<4s for the sustained-load probe; unique probe symbol for the finnhub 429 audit test; bounded resolve retry + longer per-test timeout for role_scoring). **Final: 3073 tests / 0 failed.** New ROADMAP entry for the structural test-isolation ticket (deferred, needs operator sign-off).



**✅ 2026-02-19 (iter-23): Counterfactual signals SHIPPED.** Every stale directional intent that never reached the broker is now distilled into ONE `counterfactual_signals` row BEFORE the raw intent is deleted — the signal survives compact, the intent doesn't. Signals resolve over 5m/15m/1h horizons against the fresh mark-quote resolver, producing verdicts: MISSED_WIN (direction right, block cost edge, bps ≥ +20), CORRECT_BLOCK (direction wrong, block saved us, bps ≤ -20), UNDETERMINED (noise). New `shared/counterfactuals/__init__.py` (predicate + distiller + resolver + verdict math). New `routes/counterfactuals_admin.py` with `GET /api/admin/counterfactuals/stats` (verdict/bps rollups by lane/action/block_reason/brain + top MISSED_WIN and CORRECT_BLOCK) and `POST /api/admin/counterfactuals/resolve` (manual tick). Sweeper now calls `distill_intent_to_signal` for every `directional_blocked_pre_broker` row before archive; if distillation fails (missing reference price), raw intent is preserved. Resolver piggybacks onto the auto_router reconcile sweep. New `COUNTERFACTUAL_SIGNALS` collection. 23 new tests + 127/127 total green. LIVE-VERIFIED: endpoints respond, 3 directional_blocked candidates ready to spawn signals on the next live sweep. Superseded the interim `missed_trades.py` archive-only P&L simulator.



**✅ 2026-02-19 (iter-23): Sweeper refinement — learning-capture classifier SHIPPED.** The initial sweeper preserved ANY intent without a `learning_experiences` row, which categorically was wrong — the live-learning predicate only captures real directional exposure (BUY/SELL/SHORT/COVER that reached execution). HOLD/WATCH/no_trade rows were never supposed to enter the learning tape, so the old rule created permanent retention for exactly the rows the sweeper was meant to prune. Fixed with `learning_capture_required(intent)` classifier: `action in {BUY, SELL, SHORT, COVER}` AND (broker_order_id set OR gate_state in {submitted, executed, broker_rejected}). Preserve-missing-learning rule now applies ONLY when classifier returns True. Typed `archive_reason`: `legacy_non_learning_no_trade` (HOLD/no_trade never reached broker), `directional_blocked_pre_broker` (BUY/SELL blocked upstream, kept for counterfactual analysis), `stale_never_reached_broker` (fallback). Split counters in dry-run response: `matched`, `learning_required`, `learning_not_applicable`, `preserved_missing_learning`, `preserved_active_reservation`, `eligible_for_purge`, `archived`, `deleted_distilled`, plus `archive_reason_breakdown`. 23/23 tests green (6 new classifier tests). LIVE-VERIFIED same 100-batch dry-run: now `eligible_for_purge=100` (was 0), `learning_not_applicable=100`, `preserved_missing_learning=0` (was 100), archive breakdown 96 legacy_non_learning_no_trade + 4 directional_blocked_pre_broker. The refinement unfroze exactly the rows the operator flagged.



**✅ 2026-02-19 (iter-23): Stale-intent sweeper (archive-then-delete) SHIPPED.** New `shared/intent_sweeper.py` + `POST /api/admin/intents/purge-stale` (dry_run=true default) + `GET /api/admin/intents/sweeper/status`. Doctrine: age > 6h AND (`executed != true` AND `broker_order_id` empty AND `gate_state != "submitted"`). Two per-row safety carve-outs: **(a)** active `capital_ledger.reservations[].status="open"` → PRESERVE (reconciler could still release); **(b)** `RISE_LEARNING_LOOP_ENABLED=true` AND no `learning_experiences` row → PRESERVE (learning still catching up). Learning-aware bifurcation: resolved experience → DELETE outright (knowledge distilled); everything else → ARCHIVE to `shared_intents_archive` (stamps: `archived_at`/`archive_reason`/`original_gate_state`/`archive_version="v1"`), verify insert, then delete hot row. Batch bounded (500 default / 1000 cap). 30-min background loop, ON by default (`INTENT_SWEEPER_ENABLED=true`); flip false to pause. 18 new tests + full regression cascade (56/56 green). LIVE-VERIFIED in preview: scheduler task alive, dry-run over 100 real production intents preserved 100/100 via the learning-capture-incomplete rule (exposing that May-vintage no_trade intents never got captured — a valuable operator signal). Zero deletions from dry-run — doctrine holds.



**✅ 2026-02-19 (iter-23): Master-switch wired + Webull reauth hot path SHIPPED.** **Fix 1 (master switch)**: Prior to today `mc_switch` was UI-only — the auto_router loop respected only boot-time `AUTO_ROUTER_ENABLED` env, never the runtime Mongo doc. `POST /toggle` was a placebo. Fixed: new `_is_master_switch_armed()` in `shared/auto_router.py` with 2s TTL cache, fail-CLOSED on read error. Wired into `_tick()` preflight (short-circuits ingestion when disarmed but STILL runs reconcile sweep so in-flight orders aren't stranded) AND `_route_one()` (guards the manual `/api/execution/submit` backdoor, stamps `broker_reason: master_switch_disarmed`). `_invalidate_arm_cache()` exported and called by both `/toggle` and `/arm` so operator flips take effect on the NEXT tick. State-change logs `master-switch state = ARMED/DISARMED` exactly once per transition. LIVE-VERIFIED: operator toggle in preview pod flipped state and auto_router log picked up new state 25s later. **Fix 2 (Webull reauth)**: (a) The token-read path was disk-FIRST — stale committed `webull_token.json` beat fresh Mongo mirror on every redeploy. Rewrote `_read_from_disk` in `trader/webull_auth.py` as freshness-aware: reads both tiers, picks the one with newer `created_at`, rehydrates disk if Mongo wins. (b) New `POST /api/admin/webull/reauth` endpoint — invalidates in-process cache, optionally purges stale disk file (default true), triggers Webull `POST /openapi/auth/token/create` (2FA mobile push), writes fresh token to both disk and Mongo mirror. Audit row lands in `webull_audit_log`. LIVE-VERIFIED: real 2FA push fired to operator's Webull app, new token landed in Mongo. 37/37 targeted tests green (9 new master-switch preflight + 4 new fresher-tier-wins + full regression cascade). Backend healthy.



**✅ 2026-02-19 (iter-23): P1 sidecar excision + P2 universe cleanup SHIPPED.** **P1 (surgical option B)**: Deleted 8 sidecar-loop orchestration files from `/app/trader/` — `main.py`, `broker.py`, `brains.py`, `seat.py`, `risk.py`, `feeds.py`, `feed_guard.py`, `audit.py`. Preserved the MC-support layer that the dashboard and Webull login flow depend on: `webull_auth`, `spread`, `spread_stream`, `store`, `state`, `merge_rights`, `config`. Rewrote `trader/__init__.py` as a decommission-notice docstring. Cleaned `server_modules/lifespan.py` — removed the sidecar-loop start/shutdown blocks; preserved unconditional store+state init + spread poller + `spread_stream` MQTT tile for the dashboard. Deleted 6 tests (`test_trader_{shadow_mode,risk,feed_guard,cfqs,dissent_accuracy,receipt_quote}.py`); refactored 2 tests (`test_trader_multi_ticker`, `test_trader_spread`) to drop guards on deleted modules while preserving their remaining coverage. **P2 (universe cleanup)**: New `scripts/universe_cleanup.py` — idempotent seeder ran once against `patterns_universe`. Hard-deleted 6 junk rows (`FB`, `MSFY`, `HEL31138C`, `HEL5E7DFF`, `NDBC0764B`, `NDBC349F6`). Upserted the operator-approved 20 equities (AAPL/AMD/AMZN/AVGO/BABA/GOOG/META/MSFT/NFLX/NVDA/ORCL/PLTR/SHOP/TSLA/**TSM**/**SPCX**/GME/HOTH/TEVA/PFE — TSM+SPCX replaced UBER+AMC per operator) and 20 crypto majors (existing 8 + DOGE/DOT/LTC/ATOM/ALGO/XLM/FIL/NEAR/MATIC/UNI/AAVE/MKR — Kraken-tradeable). Deactivated 31 out-of-list equities (audit history preserved via `active=False`); 0 crypto deactivations. Final state: exactly 20 active symbols per lane. New `tests/test_patterns_universe_integrity.py` — 4 regression tests locking the canonical set. **324/324 tests green** on the affected surface (trader-preserved + learning + universe + unified arm + crypto sweep + Phase C regression). Backend healthy.



**✅ 2026-02-19 (iter-23): Stage 2b — Fresh mark-price contract + Shrunk-EV floor + Bounded doctrine overlays SHIPPED.** Three landings in one pass. **(1) Mark-price fresh gate**: `outcome_resolver._fetch_mark_quote` now returns `MarkQuote(price, source, ts, is_stale)`. Horizons only resolve when `is_stale=False`. Chain: Webull last-trade (fresh) → `shared_ohlcv_bars` intraday close (fresh iff bar age ≤ 30 min) → Polygon `/v2/aggs/ticker/{t}/prev` (ALWAYS stale, diagnostic only) → None. Resolver stamps `mark_price / mark_price_source / mark_price_ts` on resolved horizons, `mark_price_stale_last_seen` breadcrumb + `skipped_stale_mark` counter on stale-only cases. Legacy `_fetch_mark_price` shim preserved for backward compat (returns price only when fresh). **(2) Shrunk-EV floor**: `lesson_proposer` now requires `shrunk_ev_bps = avg_5m * samples/(samples+100) >= 5.0` on top of Wilson≥0.50 and samples≥30. A 30-sample bucket averaging +20 bps shrinks to +4.6 → no lesson. Evidence blob carries `shrunk_ev_bps` so Kernel review sees the shrunk number. **(3) Bounded overlay engine**: new `shared/learning/doctrine_overlay.py` — `get_notional_multiplier(dims, *, db) → 0.80..1.20` (centred at 1.0) and `get_threshold_delta(dims, *, db) → -0.20..+0.20` (signed). Reads only `state=approved` lessons; exact bucket-dim match required; ±0.20 hard clamp even against runaway explicit modifier fields; 30s TTL cache; no broker access, no doctrine mutation. **NOT WIRED** into `auto_router` — ships standalone per operator directive to review math first. 81/81 tests green (13 mark-price + 18 Stage 2 + 17 overlay + full regression cascade).



**✅ 2026-02-19 (iter-23): Stage 2 finisher — equity mark-price wire SHIPPED.** `shared/learning/outcome_resolver.py::_fetch_mark_price(lane="equity", …)` was a stub returning `None`; every equity `learning_experiences` row was stuck as `skipped_missing_mark` on all three horizons. Fix: two-tier resolver — **(1) primary** Webull v2 `equity_snapshot` last-trade (reuses `shared/market_data/webull_quotes.get_quotes_client()`, sync SDK wrapped in `asyncio.to_thread`; field precedence `price → last → lastPrice → deal_price → ask`); **(2) fallback** `shared_ohlcv_bars` latest-close (any source/tf; doubles as the Polygon fallback the operator requested — grouped-daily + flatfiles feeders land here). Exception safety: Webull SDK crash falls through to bars fallback; total miss returns `None` (row stays pending). 11/11 new tests in `tests/test_learning_equity_mark_price.py` green (empty symbol, unknown lane, Webull `price` field, `ask` fallback, case-normalisation, bars fallback, ts sort, total miss, exception fall-through, zero/negative filter). Full learning suite 45/45 green. Also fixed Phase C canonical-identity regression in `capture_experience` (now stamps both `stack` AND `stack_canonical` per doctrine).


**✅ 2026-07-09 (iter-22): Learning Stage 2 SHIPPED — Bucket Analyzer + Wilson guardrails + Lesson Proposer.** Prevents early noise from becoming doctrine. New `shared/learning/bucket_analyzer.py` groups resolved experiences by `(lane, action, notional_source, rvol_band, spread_band, doctrine_band)`; writes to `learning_buckets` collection with samples / wins / losses / hit_rate / **wilson_lower** / avg_5m_bps / avg_15m_bps / avg_1h_bps. New `shared/learning/lesson_proposer.py` emits lesson proposals ONLY when the operator-mandated triple-guardrail clears: `MIN_SAMPLE_SIZE=30`, `WILSON_EDGE_FLOOR=0.50` (95% CI), `BLEED_BPS_THRESHOLD=-10`. Lessons ALWAYS land as `state="proposed"` — Kernel review (human) approves/rejects; `$setOnInsert` prevents re-proposal from resetting an approved lesson. New endpoints: `POST /admin/learning/analyze` (rebuild+propose), `GET /admin/learning/buckets`, `GET /admin/learning/lessons?state=`, `POST /admin/learning/lessons/{id}/approve|reject`. 14/14 Stage-2 tests green (Wilson math, bucketing determinism, doctrine-band recognition, guardrail composition: edge / bleed / noise / undersample) + full iter-22 cascade 127/127 green.

**✅ 2026-07-09 (iter-22): Learning-loop heartbeat resolver SHIPPED (Stage 1.5).** Piggybacks `_sweep_submitted_broker_orders` — no new scheduler. Extracted `_finish_sweep()` helper so the resolver fires EVEN WHEN no broker adapter is available (broker outage must not starve the learning tape). Counts stamped into sweep return payload: `learning_scanned`, `learning_resolved_5m`, `learning_resolved_15m`, `learning_resolved_1h`, `learning_skipped_mark`. Best-effort — any resolver failure counted as `learning_resolver_errors=1` and swallowed; the reconcile tick still returns cleanly. Rate-limit gate short-circuits both broker sweep AND resolver together (piggyback semantics). 3 new tests cover piggyback wiring + failure isolation + rate-limit gating. Full iter-22 cascade: 113/113 green.

**✅ 2026-07-09 (iter-22): Live Learning Loop Stage 1 SHIPPED.** Turns every real order (fill AND reject) into training material. Operator directive: *"Learning requires exposure. Paper mode teaches structure but it does not teach slippage, broker rejects, spread behavior, fear points, missed fills, or real market timing."* New module `shared/learning/live_loop.py` exposes `should_learn_from(intent)` predicate (BUY/SELL + notional>0 + lane ∈ {equity, crypto}) + `capture_experience()` writer that snapshots the FULL intent context (features + doctrine packet) at execution time. Hooked into `auto_router._route_one` at BOTH the success terminal path AND the broker-reject terminal path. Best-effort — a learning-write failure never blocks the order pipeline. New `shared/learning/outcome_resolver.py` walks the horizon ladder (5m/15m/1h), computes signed basis-points-of-move, stamps `win` at the 5m mark. Crypto mark-price wired via the existing Kraken public ticker; equity is stubbed for Stage 2. New admin endpoints: `GET /api/admin/learning/stats`, `GET /api/admin/learning/experiences`, `POST /api/admin/learning/resolve`. 18/18 Stage-1 tests green + 110/110 full iter-22 cascade green. Preview smoke: 8 experiences already captured from test runs.

**✅ 2026-07-09 (iter-22): P2 crypto reconcile sweep SHIPPED.** `_sweep_submitted_broker_orders` in `shared/auto_router.py` used to hard-code `lane: "equity"` and only poll Webull — 127+ stuck crypto intents dating back to 2026-07-05 lived forever in `submitted` state. Fix: added `KrakenLiveAdapter.get_order(txid)` (delegates to the existing `shared/crypto/kraken.py::query_order` which already returns the Webull-normalized shape). Sweep now iterates per-lane adapters; each broker's outage is fault-isolated (one broker down doesn't wedge the other's reconcile). Log line updated to include `equity=%d crypto=%d` breakdown. Prod probe: 127 stuck crypto intents ready to be walked by the sweep. 5/5 new crypto sweep tests + 38/38 regression tests + 92/92 in the full iter-22 cascade all green.

**✅ 2026-07-09 (iter-22): shared-package parent-attr leak in `_apply_patches` FIXED.** Discovered while chasing a downstream StopIteration in `test_success_stamps_final_notional_usd_on_intent`. Root cause: after any earlier test file boots the full FastAPI app via `AsyncClient(transport=ASGITransport(app=app))`, the `shared` package has attributes `shared.seat` / `shared.risk` / `shared.executions` bound to the REAL modules. `_route_one`'s inline `from shared import executions, risk, seat` reads the parent-package attribute, NOT `sys.modules`, so `patch.dict(sys.modules, ...)` mocks were silently ignored. Fix: `_apply_patches` now patches BOTH surfaces via `patch.object(shared, "seat", mock, create=True)` + the existing `patch.dict(sys.modules, ...)`. Whichever path `_route_one` uses now lands on the mock.

**✅ 2026-07-09 (iter-22): MC Kraken env-var fallback SHIPPED.** MC's `shared/crypto/kraken.py::get_active_keys_status()` now falls back to `KRAKEN_API_KEY` + `KRAKEN_API_SECRET` env vars when the encrypted `kraken_credentials` singleton is missing / malformed / undecryptable — bridges the historical trader-sidecar-vs-MC key-source gap. Emits a WARNING log on every use so the operator sees the temporary bridge is in play. `arm/status` payload now includes `crypto_broker_ready: bool`, `crypto_creds_source ∈ {mongo_singleton, env_fallback, null}`, and `crypto_creds_detail`. Preview pod correctly reports `crypto_broker_ready: false` (no env vars here); production (env vars ARE set) will auto-flip to true with `source="env_fallback"` on first Kraken order attempt. 7/7 fallback tests + 7/7 shadow-mode tests + 12/12 unified-arm tests = 26/26 iter-22 green (34/34 including cascade). Remove fallback after operator migrates keys via `POST /api/admin/kraken/connect`.

**✅ 2026-07-09 (iter-22): Sidecar demoted to shadow — "one broker door" doctrine SHIPPED.** Operator directive: *"There is only one broker door. MC owns it. Sidecar cannot submit orders."* Both `trader/broker.py::kraken_market_order` and `webull_market_order` now short-circuit before the network call when `TRADER_ENABLED` is falsy (production default) and return `{shadow_only: true, ...}` synthetic receipts. `trader/main.py` detects shadow receipts and writes `broker_status="shadow_only"` on the audit row so `receipts.jsonl` stays diagnostically useful but no fill is ever claimed. `/admin/trading/arm/status` now surfaces `trader_authoritative: bool` and `broker_door_owner ∈ {mc_only, sidecar_and_mc_both}` so the operator sees the true door state in the same tile as the master arm. Live smoke on preview: `broker_door_owner=mc_only`. Decommission path is staged per operator: shadow → observe → strip credentials → delete UI refs → delete `/app/trader`. 7/7 shadow-mode tests green + 12/12 unified-arm tests still green.

**✅ 2026-07-09 (iter-22): Unified arm surface (`/admin/trading/arm` + `/lane`) SHIPPED.** Diagnosis: production had THREE independent "master switches" (env `BROKER_LIVE_ORDER_ENABLED`, Mongo `trading_controls.enabled`, Mongo `runtime_flags.master_trading_switch`). Env #1 showed LIVE in the Flags UI but the two Mongo gates BOTH defaulted to CLOSED because no docs existed — trader-sidecar receipts had been logging `master_switch_disarmed` on every cycle since 2026-07-02 as a result. Fix: new `POST /admin/trading/arm` flips BOTH Mongo master docs in one call (with optional `lanes: {equity, crypto}` merge-update); new `POST /admin/trading/lane` for fine-grained per-lane control without touching master arm; `GET /admin/trading/arm/status` returns the combined 5-field truth (env×2, mc_switch, trader_switch, lanes×2, plus `all_armed` / `will_fire` / `trader_will_fire` summaries). Every flip writes one unified audit row into `trading_controls_audit` with source-tagged `unified_arm` or `lane_toggle`, including pre/post state of every switch. 12/12 tests green. Live-endpoint smoke returned `all_armed: false` correctly (matches the operator's "dead stick" observation — now honestly reported).

**✅ 2026-07-09 (iter-22): Revised `assign_micro_notional` rule SHIPPED.** Replaced the earlier "specific triple" soft-gate with the operator's simpler broader rule: any directional (BUY/SELL) intent with `notional_usd=null` → if doctrine reports ANY `failed_checks` → $1.00 probe with `notional_source="micro_probe_failed_quality"`; if doctrine is clean → $5.00 with `notional_source="micro_default"`. Brain-sized intents (legacy or v3) always win. 9/9 micro-notional tests green including the new "brain-sized intent survives failed_checks" case.

**✅ 2026-07-09 (iter-22): sys.modules leak in `test_live_execution_path.py` FIXED.** After the file's `patch.dict` cycle, subsequent test files' `monkeypatch.setattr("shared.market_hours.<attr>", ...)` was tracking the entry correctly but the value inside `_route_one`'s runtime `from shared.market_hours import ...` was the stale original — resolver picked up a different module ref. Fix lives in the consumer (`test_micro_notional_fallback::_wire_common_patches` now does a defensive `import shared.<mod>` of every module it will patch, re-anchoring `sys.modules` before setattr). 53/53 tests green when run in sequence across three files.

**✅ 2026-07-09: P0 Cached brain_runtime_metrics SHIPPED.** `/api/admin/runtime/{brain}/status` now averages ~100ms across all 4 brains reading from a tiny per-brain micro-doc.

**✅ 2026-07-09: P1a notional_source failure-path stamping SHIPPED.** All 8 failure-path `$set` blocks in `shared/auto_router.py::_route_one` carry `notional_source`.

**✅ 2026-02-20: Dual-path `has_volume_evidence` + `rvol_acceleration` / `trend_score` in `session_features` SHIPPED.** See CHANGELOG head for details.

**✅ 2026-02-20: P1 brain-runtime `latest_intent_ts` + `latest_intent_age_s` SHIPPED.** See CHANGELOG head — silent-write-halt detection now trivially operator-visible on `/api/admin/runtime/{brain}/status`.

**✅ 2026-02-20: Per-Lane Capital Cap Ledger MODULE SHIPPED** (22 tests green, endpoint live). **✅ Executor wire-up ALSO SHIPPED** — reserve-before-broker in `_route_one`, release on broker terminal reject (both immediate exception and reconcile-sweep paths) and position close, scheduled stale sweeper (5min tick, 30min equity / 60min crypto thresholds). 5 wiring integration tests green.

**✅ 2026-02-20: `market_regime` + `velocity_5m` SHIPPED (Follow-up A COMPLETE).** `session_features_v2_pending` group deleted from coverage report. Live coverage: market_regime 72.7%, velocity_5m 54.5%.

**✅ 2026-02-20: P1 silent-write-halt trace — VERIFIED NO ACTIVE HALT.** All 4 brains writing on their ~3.5min cadence at trace time. Cadence-drift sentinel added (piggybacks on fingerprint tick, logs WARNING at `max(3×median_gap, 600s)` drift).

**✅ 2026-02-20: P2 Distribution Snapshot Job SHIPPED.** `shared/session_fingerprint.py` + 3 admin endpoints under `/api/admin/fingerprints/`. 15-min windows, aligned + idempotent. Captures gate_state / quality / top-labels+reasons+objections / execution_ready_rate / gate_pass_rates / confidence+rvol+gap percentiles / market_regime dist. First live fingerprint showed 6-intent Camino window all C_QUALITY, all blocked on volume — exactly the funnel-choke signal the PRD wanted.

**✅ 2026-02-20: Fingerprint Diffing Tool SHIPPED.** `GET /api/admin/fingerprints/diff` + `FingerprintDiffPanel` lazy-mounted on `/admin/diagnostics`. Aggregates two ranges of fingerprints (BEFORE / AFTER a doctrine change) into composites and surfaces the deltas: exec_ready_rate, gate_pass_rates, quality_dist, top_fail_reasons (with explicit new/dropped/count-deltas), risk_multiplier_p50, confidence+rvol+gap percentiles. Counts are exact sums; percentiles are weighted-mean approximations (documented in response `note`). 14 tests green (25/25 across fingerprint module).

**⚠️ 2026-02-20: OPEN OBSERVATION — Crypto lane silent in fingerprints.** All 4 brains' crypto-lane fingerprints show `intent_count=0` for the trailing hour, despite Camino holding the crypto seat and the market being open 24/7. Not blocking; deferred. First step for future trace: `/api/admin/intents?lane=crypto&limit=50` — if empty, check `_route_one` upstream signal path.

**✅ 2026-02-20: P3 Cleanup SHIPPED.** (a) 614 synthetic ToS rows swept from `shared_indicator_snapshots` (only NVDA + SPY real tickers remain in thinkorswim source). (b) Category C assertion-drift eradicated — brought failing tests from ~60 down to 0 real failures. Bulk brain-name migration across 37 test files. Dead-endpoint test files (last-submit-block, heartbeat-status) deleted. Alias-translator contract, refactored-code inspection targets, and schema-drift seed rows all updated. (c) 4 production bugs surfaced & fixed: cross-brain-memories KeyError guard, intent-clearance-funnel Phase-C stack grouping, external_signals dedup partial index, sidecar-checkin docstring. (d) 65,572 legacy brain-name rows migrated in data-plane collections; audit-log tables intentionally preserved.

**🔵 Known P4 (test hygiene, deferred)**: `test_broker_error_taxonomy` × 4 flakes on xdist parallel workers due to shared collection state — all pass in serial and in isolation. Add a per-test collection prefix + fixture isolation before enabling xdist in CI.

**✅ 2026-02-20: Kraken 1d bar feeder SHIPPED.** `source="kraken_pro"` `tf="1d"` bars now land on boot + hourly poll. Crypto RVOL 20-day baseline gap closed.

**✅ 2026-02-20: Feature Coverage Report tuning SHIPPED.** Stale threshold 120→180min (3× poll interval); `session_features_v2` group updated with `rvol_acceleration`/`trend_score` now-shipped fields.

**Extend `build_snapshot()` in `shared/technicals.py` to compute doctrine-facing fields, thread them through the runner-emit path.**

#### Root cause (refined 2026-02-19 late — supersedes earlier "wire enrich_equity_doctrine_snapshot" phrasing)
The brains DO have access to real market data — but they read it from a Mongo cache (`shared_indicator_snapshots`), NOT from direct broker calls. That cache is populated event-driven by external feeders POSTing OHLCV bars to `/api/ingest/ohlcv`, which triggers `shared/technicals.py::_recompute_snapshot` to rebuild the indicators block.

The gap: `build_snapshot(bars)` currently computes a base set of indicators (RSI, EMA, VWAP, etc.) but does NOT compute the doctrine-facing fields the doctrine actually reads (`gap_pct`, `relative_volume`, `market_regime`, `vwap_distance_pct`, `velocity_5m`, `rvol_acceleration`). So brains have RICH cached indicator data available, but when their tick calls `_build_intent_body` to construct the intent's `doctrine_snapshot`, the doctrine-facing fields are absent → intent lands at MC with `spread_bps=999 (sentinel)` and no other doctrine data → NO_DATA / "NO PROVENANCE" on every intent.

This is architecture B (in-process shared runners reading from a Mongo cache) working exactly as designed — the cache just doesn't carry the doctrine fields yet.

#### Fix scope
1. **Extend `shared/technicals.py::build_snapshot(bars)`** with a companion function (or inline additions) that computes:
   - `gap_pct` — (current_open - prev_close) / prev_close × 100
   - `relative_volume` — current_bar_volume / rolling_N_bar_avg_volume
   - `market_regime` — trend/chop classifier from the same bar window (weak/strong/unknown)
   - `vwap_distance_pct` — (last_close - vwap) / vwap × 100
   - `velocity_5m` — pct change over last 5 bars (or however "5m" maps to the tf)
   - `rvol_acceleration` — delta of RVOL over the last N bars

   Stamp these into the `indicators` block of the doc `_recompute_snapshot` upserts.

2. **Thread the fields through `_build_intent_body`** in `shared/brains/_runner_core.py:225-229` so the intent's `doctrine_snapshot` payload includes them. Read from `snap["indicators"]`, promote to the top level of `doctrine_snapshot`.

3. **`spread_bps` special case:** OHLCV bars don't carry bid/ask, so `spread_bps` cannot be computed from the bar cache. It stays with the existing `enrich_snapshot_spread` path (brain-supplied, MC-derived, kraken-direct, or sentinel). The NO_DATA short-circuit correctly handles sentinel spread — this is not a doctrine blocker.

4. **Add test**: given a synthetic bar sequence, verify `build_snapshot()` produces expected doctrine-field values and that a downstream `_build_intent_body` call propagates them into the intent's `doctrine_snapshot`.

#### Do NOT do before this ships
Any additional NO_DATA short-circuit patches, provenance dashboards, or telemetry endpoints. The two NO_DATA fixes shipped this session (base + sentinel-spread) correctly render the current state as "no data" — that's the honest UI. The right next move is making REAL data flow so the honest state becomes "have data" instead of continuing to make the "no data" state prettier.

#### Critical dependency note (unchanged from earlier PRD version)
**The 2026-02-19 large-cap doctrine work (Universe Classifier, Doctrine Registry, VWAP/velocity/RVOL/EMA scoring, direction.strategy_bias) is inert until this fix lands.** All of it reads its scoring signals from doctrine-facing snapshot fields that don't yet flow through the runner path.

#### Also open (separate P0 — investigate but rank AFTER the doctrine-field flow-through)
**Silent-write-failure diagnosis on the runner path.** Post-deploy check showed Camino's runner internal counter (`intent_count: 267 since deploy`) incrementing while direct DB check showed last actual Camino intent 11h ago. The runner's success signal is measured at emission-decision level, not DB-write-confirmed level. Fix scope:
   - Trace where `_intent_loop`'s counter increments relative to the `submit_intent_in_process` return
   - Add a "last DB-confirmed ingest_ts for this stack" field to `_build_in_process_status` payload
   - Fix the `ingest_ts` cross-type comparison bug in `routes/brain_runtime.py:224-225` (currently uses `.isoformat()` string cutoff — potentially returns inflated counts if `ingest_ts` is Date-typed on prod)

#### Warmup task (P2 — small, do before the P0 build_snapshot extension for a low-cost win)
**Add per-brain `STRATEGY_SHA` to the identity block.** The current identity panel conflates two different kinds of identity:
   - **Deploy identity** (correctly shared across brains): `git_sha`, DB, platform, MC URL. `git_sha` is currently stubbed to `"in-process"` — should be the real deploy SHA (separate small fix worth doing while touching this area).
   - **Brain identity** (must differ per brain): which `strategy.py` is running. NOT surfaced anywhere today, even though the strategy files are physically distinct per brain.

Fix: add `strategy_sha` field to the identity block, computed as `hashlib.sha256(open("shared/brains/<brain>/strategy.py").read_bytes()).hexdigest()[:12]`. Real per-brain differentiation grounded in what's actually different code, not cosmetic name-rendering.

**Drift alert value:** if two brains ever show the same `strategy_sha`, that's a genuine bug (strategy files accidentally converged via bad refactor, symlink, or copy-paste). Consider adding a small assertion at boot: `assert len({strategy_sha(b) for b in KNOWN_BRAINS}) == len(KNOWN_BRAINS), "strategy_sha collision across brains"`.

Precedent: `broker_mode` already differs per brain and does real differentiation work today. Extend that pattern, don't cosmetically rename `git_sha`.

**✅ SHIPPED this session** (2026-02-19 late-late): `shared/brains/_strategy_identity.py` (uncached, always-correct hash), boot-time collision guard in `server_modules/lifespan.py`, identity block in `routes/brain_runtime.py` now carries `strategy_sha`. All 4 brains show distinct hashes live. 7/7 tripwires green. Rides out with the sentinel-spread fix on next redeploy.

#### Refactor lane (P3 — warmup material for next session, not urgent)
Ranked by refactor-SAFETY (line count alone lies — some 1000-line files are one coherent concern, some 400-line files bundle 3 unrelated concerns).

**Safe splits — clear seams, low behavior-change risk:**
- `shared/intents.py` (2102 lines) — extract auto-dry-run hook (lines 72-131, ~60 lines) to `shared/intents/dry_run_hook.py`. Trivially isolated, single entrypoint.
- `shared/auto_router.py` (1213 lines) — extract broker reconciliation sweep (lines 702-990, ~290 lines) to `shared/auto_router/broker_reconciler.py`. Self-contained, single caller.
- `shared/roster.py` (917 lines) — extract Pydantic models (lines 352+) to `shared/roster/models.py`. Pure classes, no runtime behavior.

**Do NOT refactor yet:**
- `shared/doctrine/large_cap_doctrine.py` (774 lines) — just added ~200 lines this session; wait for stability before splitting.
- `shared/technicals.py` (717 lines) — is the P0 target; refactoring now fights the P0 work. Extend first, split later if needed.
- `server_modules/lifespan.py` (854 lines) — boot-order dependencies not visible from static scans, splitting risks subtle races.
- `routes/admin_trader.py` (722 lines) — URL-path stability; only touch if intentionally renaming routes.
- `shared/mc_shelly.py` (656 lines) — audit log, shape must not drift.
- `shared/broker/webull.py` (1430 lines) — cohesive SDK wrapper; splitting fragments retry/error taxonomy.

**Cohesive-and-happy** (no split recommended): `positions.py`, `opinions.py`, `broker_router.py`, `crypto/kraken.py`.

Recommended warmup sequence if picking up refactor before the P0s: (1) `dry_run_hook` extract, (2) `broker_reconciler` extract, (3) `roster/models` extract. Combined ~30-45 min, removes ~500 lines from the three biggest active files without changing a single function's behavior. Tests-first, verifiable by import-shape parity.

---

### ✅ Universe classifier + doctrine router pattern + NO_DATA short-circuits — SHIPPED 2026-02-19

See CHANGELOG for full details. Summary:
- `shared/doctrine/universe_classifier.py` — pure symbol → universe-class dispatch (`CRYPTO`/`SMALL_CAP_MOMENTUM`/`LARGE_CAP`/`ETF`/`UNKNOWN`). Operator-vetted: no silent lane fallback — unclassified equities fail loud into `UNKNOWN` → NO_DATA (never a scored default doctrine).
- `shared/doctrine/registry.py` — 4 builders wired (large-cap, small-cap momentum with strategy dispatch, ETF, crypto). UNKNOWN → NO_DATA short-circuit.
- `shared/doctrine/large_cap_doctrine.py` enhanced with VWAP tilt / 5m velocity / RVOL acceleration / EMA-stack scoring signals + `direction.strategy_bias ∈ {BUY, SELL, NEUTRAL}` derivation. Blocked by the ingest-wiring fix above.
- `shared/doctrine/large_cap_doctrine.py` NO_DATA short-circuit — symmetric with `base_labels.py` and `brain_sidecars.py`; empty/failed-enrichment snapshots return quality="NO_DATA" with neutral seats + `no_data=True` flag instead of an identical scored REJECT (the operator-screenshotted bug).
- `lane_doctrine_router.py` collapsed to a thin lane-guard + registry-delegation shim.
- P1 hotfixes: `[(symbol, 1), (ingest_ts, -1)]` compound index on `shared_intents`; `meta_routes.py` 4-tuple unpacking bug on `BRAIN_ROSTER`.
- Test cleanup: 47 real backend test failures eliminated (34 roster-rename sweep, 3 router-inversion updates, 4 dead-path file deletions, 2 in-place test deletions, 1 wiring-assertion fix, 3 for the doctrine changes).
- 66/66 doctrine tests green, 0 lint errors, 0 real regressions introduced.

**Follow-up work (P2, blocked by the P0 above):**
- Observe 1-2 weeks of live RTH Trade Tape data to tune large-cap doctrine weights (only meaningful AFTER the enricher wiring lands and real signals start flowing).
- Consider dedicated ETF doctrine (currently ETFs route through large-cap builder) once ETF sample size supports Patent J graduation.
- OpenMythos training on the RISE JSONL substrate that's been accumulating.

**Also open (unchanged from prior sessions):**
- Mongo timeout in `risk_check.py` — separate production blocker, kept on the board.
- 13 remaining Category C test failures (assertion drift within features that still exist — conflict_memory, intent_summary, sidecar audit-write, broker `stamped_at`, etc.) — per-test judgement calls, separate ticket.
- `test_live_execution_path.py` cross-suite test pollution — passes 38/38 in isolation, fails 8-10 in mixed-suite runs. Recurring issue flagged in handoff, needs dedicated pollution-source diagnosis.


### ✅ Session 2026-07-07 (late): Witness W/L resolver activated (was dormant 8 days)

**Built:**
- `backend/verifier/witness_resolver.py` — full MVP resolver: classification, aggregation, promotion state machine, idempotent DB path
- `backend/routes/admin_external_signals.py` — `POST /api/admin/verifier/resolve-witnesses/{source}` endpoint with real price-history fetcher wired to `shared_ohlcv_bars` (broker-primary priority)
- 32 tests, all passing, 0 lint errors

**Deferred to follow-up:**
- Orthogonality tracking (MVP uses raw win rate)
- Scheduled nightly execution (MVP is admin-trigger only)
- Regime-conditional scoring + drawdown per stance
- Full `verified_alpha` attribution vs baseline

**Ships on next redeploy.** Operator can then hit the endpoint (`?dry_run=true` first) and observe polygon resolutions from the 741 accumulated rows.


### ✅ Session 2026-07-06 (evening): Watchlist cull to 20+20 + silent-brain incident resolved + doctrine NO_DATA short-circuit staged

**Live prod actions this session:**
- Trimmed `patterns_universe` from 48 equity + 8 crypto → 20 + 20 by daily $ volume (via API calls against prod, already live)
- Confirmed brain intent stream recovered post-redeploy (NVDA, MSFT HOLD intents flowing)
- Operator has per-lane kill switches, zero live-money exposure through the incident

**Staged code changes (preview, ship on next redeploy):**
- Option A/B doctrine work: NO_DATA short-circuit + enricher status stamping (kills the manufactured `-26%/-38%/-88%/-80%` fingerprint on symbols with failed enrichment)
- Paper/dry_run cosmetic cleanup (WebullConnect ENV_OPTIONS, LaneExecutionTogglesPanel copy)
- Three dead env flags removed from `.env`
- 32 test cases added (`test_doctrine_no_data_short_circuit.py`, `test_equity_enricher_status_stamp.py`), all passing

**Outstanding tech debt (not blocking):**
- Missing `shared_intents.symbol` index causes `?symbol=X` timeouts on prod's admin Intents page
- `meta_routes.py:42` 3-tuple/4-tuple unpack drift breaks `/api/admin/neutral-brains/status` observability
- `deploy_mode="execute"` typo on prod (canonical is `"execution"`)
- Runtime log source on Emergent deploy panel not yet identified — only build logs visible


### ✅ Paper/dry_run mode elimination + dead env-flag cleanup (2026-07-06)

**Operator directive:** eliminate `paper` and `dry_run` from the codebase
— system is single-stack, LIVE-armed for real money. Also clean up old
env slots that aren't used anymore.

**Fixed:**
- P0: `test_neutral_brain_identity_stamp.py` IndentationError from a
  botched search-and-replace (stray `result["errors"]` line 191).
  Tests: 13/13 passing.
- `.env`: added `RISEDUAL_BROKER_MODE="live"` — check-ins now stamp
  `broker_mode=live` instead of `unset`/`paper`. Verified via
  `/api/admin/runtime/sidecar-checkin`.
- Removed three retired enforce-flag env vars: `PHASE6_ENFORCE_ENABLED`,
  `CAMARO_EXECUTOR_ENFORCE_ENABLED`, `CHEVELLE_AUTHORITY_ENABLED`
  (declared dead by `flags.py`'s 2026-02-17 authority-on-seats rev3).
- UI copy: `WebullConnect` no longer offers `"paper"` in ENV_OPTIONS;
  `LaneExecutionTogglesPanel` stripped the `(or paper fills for Alpaca)`
  copy from the enable-lane dialog.
- `platform_survival.broker_verify_receipt` docstring updated to "live"
  only.

**Kept as ACTIVE (not dead):** `PARADOX_MA_CANARY_*` (canary runner),
`OPPONENT_MODE` (role_health / paradox_record audit tier), `DEPLOY_MODE`
(flags/diagnostics/meta_routes), `BRAIN_ENV_NAME` (legacy fallback in
runner.py), `LADDER_MICRO_PAPER_USD` (sizing ladder route, NOT broker
mode — the ladder is the sizing authority).

**Gate check:** `BAD_BROKER_MODE` in `platform_survival.py:93` strictly
rejects any `broker_mode != "live"` — enforcement is unchanged and now
matches reality.


### ✅ Shelly rewrite — lean learning recorder ONLY (2026-07-06)

**Operator directive:** Shelly must be a lean learning recorder, not
a parallel MC. The Evidence Store is the learning substrate; Shelly
feeds MC better evidence.

**Deleted:** `backend/shelly/` package (LocalShelly, MCShelly cross-brain
reasoning, verified-facts L3, RISEDUAL wiki L6, MEMORY.md renderer,
Phase-2 embeddings, pipeline orchestrator), `backend/shared/shelly_bus/`
(trust-scored brain memory proposals), `routes/shelly_admin_extension.py`,
5 dependent test files. Router registry unwired.

**Kept:** `backend/shared/mc_shelly.py` — LEARNING_EVENTS whitelist
(position_opened, position_closed, order_routed, order_filled,
outcome_resolved, rotation) + 90d TTL; `routes/brain_memory_ingest.py`
(separate `brain_memories` collection).

**Untouched (per directive):** `auto_router`, `seat`, `risk`, `roster`,
`brokers` (Webull/Kraken/Public), `intents`, `live_positions`,
`doctrine_injection`. All continue calling `shared.mc_shelly.record_async`.

**Snapshot:** git tag `pre-shelly-rewrite` / branch
`snapshot/pre-shelly-rewrite`. Rollback via checkout.


### ✅ Option C: Kraken manual reconcile endpoint (2026-07-06)

Operator flipped from Option A → C ("build the adapter capability +
manual-trigger endpoint, don't auto-fire on preview without live
smoke-test capability").

Shipped:
- `shared/crypto/kraken.py::query_order(txid, pub, priv)` —
  normalizes Kraken's `/0/private/QueryOrders` response to the same
  shape `WebullAdapter.get_order` returns. Single decode point for
  Kraken schema.
- `routes/kraken_manual_reconcile.py::POST /api/admin/kraken-reconcile/reconcile-intent`
  — operator-triggered, single-intent reconciliation. Reuses the
  auto_router sweep's state-machine transitions (Filled / Terminal /
  Transient-under-cap / Working). Stamps `reconciled_manually:True`
  for audit trail.
- 13 new pytests: 6 adapter-mapper (fixtures locked to Kraken's
  documented QueryOrders format) + 7 endpoint state-machine.
- **65/65 tests pass.**
- Live-smoke verified on preview: not-found path returns clean
  diagnostic; equity-lane intent correctly rejected with HTTP 400.

**Monday plan:** operator invokes endpoint selectively when they
see a stuck crypto intent. First live Kraken response contact is a
deliberate operator action, not a background firehose.

**Follow-up:** promote to auto-sweep after Monday's data confirms
adapter fixtures match reality. ~15 LOC + 4 tests.



### ✅ P1 shipped — Broker reconciliation sweep (2026-07-06)

**Operator sign-off:** Option 2 + `pending` + near-boundary log +
taxonomy ordering fix.

`_sweep_submitted_broker_orders()` in `auto_router.py` polls Webull
every scheduled tick for `gate_state='submitted'` equity intents.
Filled → `gate_state='filled'` + fill data stamped. Rejected via
`classify()` from existing `broker_error_taxonomy`: terminal buckets
or retry_count ≥ 3 → `broker_rejected`; transient under cap →
`gate_state='pending'` (canonical fresh-emission state, requeues
identically to a fresh intent). Cap 25/tick, `.max_time_ms(3000)`,
15s outer wait_for. Broker exception isolated per-intent.

**Rate-limit gate** (smoke-test finding): `intents.py` calls
`force_one_tick()` on every intent insert (~50ms latency opt).
Without a gate, brain emission bursts would trigger reconcile
storms. Sweep now skips itself if last run was <25s ago.

**Taxonomy ordering fix** (surfaced by reconcile tests): the
`invalid_order_args` catch-all (`"http status: 4"`) was greedily
misclassifying Webull's real 429 format (`HTTP Status: 429,
TOO_MANY_REQUESTS`) as **terminal**, defeating the retry cap for
the single most likely RTH rejection. Moved `rate_limited` block
before `invalid_order_args`. Regression anchor test added.

**Coverage:** 51/51 tests pass. 10 new reconcile-specific + 1
taxonomy regression.

**Live-verified on preview:** synthetic stuck intent → polled by
next scheduled tick → Webull 417 exception caught → `errors=1`,
`no_change=0`, intent state preserved. 5 concurrent
`force_one_tick()` calls: only 1 sweep ran, 4 silently rate-limited.



### ✅ Dead-tile cleanup: 3 diagnostics surfaces removed (2026-07-06)

**Operator directive:** *"nothing here has ever earned its keep."*

Screenshot showed **Decisions Feed**, **Promotion Artifact / Evidence
Feed**, and **Brain Health** tiles throwing Mongo Atlas timeouts in
prod. None had worked since install. Rather than hardening dead
queries with `.max_time_ms()` bounds, removed:

- `backend/shared/decisions_feed.py` (~390 LOC)
- `backend/shared/promotion_artifact_report.py`
- `backend/routes/brain_health.py`
- `frontend/src/components/BrainHealthTile.jsx`
- `frontend/src/components/PromotionArtifactPanel.jsx`
- `DecisionsFeed()` + its constants from `pages/Diagnostics.jsx`
- 4 orphaned test files, trimmed 1 more
- 3 router-registry imports + include_router calls

Snapshot `pre-dead-tile-cleanup` created before deletion. 38/38
regression tests pass. Diagnostics page renders clean, deleted
endpoints return 404 as expected. Three fewer heavy Atlas queries
per page load.


**Verified:** backend boots clean, kept endpoints 200, deleted routes

### ✅ Equity market-closed pre-flight gate (2026-07-06)

**Operator directive:** *"Market closed is not a broker error. It is
a known routing condition."*

Added a lane-aware pre-flight in `auto_router.py::_route_one` between
Risk and Broker: equity + market closed → skip Webull entirely, stamp
`gate_state=blocked`, `broker_reason=market_closed_preflight`,
`broker_error_bucket=market_closed`, write ONE audit row with
`broker_status=market_closed_preflight` (NOT the `broker_error:`
prefix). Consults `is_equity_rth()` or `is_equity_extended_hours()`
based on the runtime flag. Crypto untouched.

Also flipped `RISEDUAL_BRACKET_OUTCOMES_ENABLED=true` in backend/.env
so Advisor Performance / Win-rate tiles populate.

**Live impact:** eliminated ~2,000 wasted Webull calls/day, 13k+ HTTP
417 log lines, and rising 429 rate-limit risk that threatened
Monday's opening bell. 28/28 regression tests pass (22 existing +
6 new in section 11 of `test_live_execution_path.py`).


404, no shelly-related pytest failures.



### ✅ Time-drift test fix (2026-02-XX)

`backend/tests/test_trader_dissent_accuracy.py` — `_seed_cycle` and both
`store.record_execution` calls now use `datetime.now(timezone.utc).isoformat()`
via a new `_now_iso()` helper instead of hardcoded `"2026-07-02T12:00:00+00:00"`
strings. Root cause: the D and E aggregator endpoints filter rows to
`now - window_hours` (24h default), so hardcoded seed timestamps aged out
of the lookback window once real wall-clock time passed 2026-07-03.

Verified: all 4 tests in the file green. Full 88-test regression across
this session's newly-added trader test files (CFQS, phase admin, doctrine
kill switch, multi-ticker, purge, spread-quality, warmup, spread) also
green. No production code touched.


### ✅ Warmup progress endpoint (2026-07-03)

`GET /api/admin/trader/warmup-progress` — per-symbol OHLCV bar counts
across the configured universe, against the research-layer's 50-bar
warmup floor. Answers the operator question "why isn't NVDA firing
yet?" during the first ~50 minutes after a redeploy in one glance.

**Deliberately SEPARATE from `/api/admin/trader/status`.** The status
endpoint promises to serve even when Atlas is unreachable (reads only
local SQLite + in-memory state). Adding a Mongo query would break
that promise. The warmup endpoint hits Mongo but degrades gracefully
via `asyncio.wait_for(timeout=8s)` + soft-error envelope, matching the
failure-surface doctrine.

**Response shape:** per-symbol `bars/required/ready/pct_complete`, plus
top-level `all_ready`, `ready_count`, `total_symbols`. Not-ready symbols
sort first so operators see blockers at the top.

**7 pytests:** happy path (all ready), partial readiness (blockers-first
sort), pct-complete boundaries (0/50/100/200 → 0/50/100/100), Atlas
timeout soft-degrade, Atlas exception soft-degrade, empty universe →
vacuous all_ready, singular-env-var backward-compat.

**Doctrine pin — Atlas-touching endpoint contract:**
Any new endpoint that hits Atlas must either (a) tolerate slow/dead
Atlas gracefully with a soft-error envelope, or (b) live on a page the
operator can tolerate not seeing during Atlas incidents. Never let a
dashboard tile freeze the whole app. Precedents: `/parabolic-phase/phases`,
`/mc/shelly/events`, this endpoint.

### ✅ Spread-quality guard + intents purge (2026-07-03)

**Root cause found in prod (with the operator):** Neutral brains were
emitting HOLD on every tick because three separate scorers treated
stale/sentinel `spread_bps` values (500-9999 bps from after-hours
quotes) as if they were real wide spreads. Every intent's HOLD/OBSERVE
hypothesis scored 1.0 while BUY/SELL capped near zero, and the doctrine
layer stamped SPREAD_TOO_WIDE + REJECT downstream — three days of
"not trading" despite `WILL_FIRE: YES`.

**Three-file fix:**
1. `backend/shared/doctrine/base_labels.py` — check `spread_quality`
   before applying SPREAD_TOO_WIDE label; emit informational
   `SPREAD_QUALITY_UNKNOWN` when stale/sentinel, no score deduction.
2. `backend/shared/doctrine/large_cap_doctrine.py` — same guard.
3. `external/brains/brain_core.py::_build_hypotheses` — substitute
   `spread_bps=25.0` (neutral) at the top of the function when
   `spread_quality ∈ {stale, sentinel}`, so HOLD/OBSERVE don't pin to 1.0.

**12 pytests** in `test_spread_quality_guard.py` cover live/stale/sentinel
across all three scorers + backward-compat for missing `spread_quality`.

**Intents purge endpoint** — new admin-only cleanup at
`POST /api/admin/intents/purge-non-executable`. Dry-run by default,
refuses to touch executed history or in-flight rows. 8 pytests pin
the safety invariants. Preview run cleared 48,341 stale HOLD intents.

## Doctrine — Narrow Universe (locked 2026-07-03)

**Depth over breadth.** Stage 1 constrains the sidecar to 2 tickers per
lane so every failure becomes a case study, every brain sees the same
market data (making the dissent tracker + CFQS + confidence re-baseline
harness statistically meaningful), and the operator swaps "why didn't
anything trade today?" for the tractable "why didn't NVDA trade at 10:32?"

**Operator picks (Stage 1):**
- Equity: `NVDA, SPY` — NVDA stresses breakout/momentum brains, SPY
  stresses trend/mean-reversion brains
- Crypto: `XBTUSD, SOLUSD` — BTC for large-stable-trend regime, SOL
  for high-vol pump-and-fade regime

**Config surface:**
- `TRADER_EQUITY_TICKERS` (comma-separated, default falls back to
  singular `TRADER_EQUITY_TICKER`)
- `TRADER_CRYPTO_PAIRS` (same shape)
- `TRADER_EQUITY_SPREAD_TICKERS` / `TRADER_SPREAD_PAIRS` — kept aligned
  with the trading universe so the spread poller never watches symbols
  the brains don't trade
- `AUTO_ROUTER_ENABLED=false` on prod — Path 1 (MC auto-router) retired
  when TRADER_ENABLED=true, sidecar is the sole trading authority

**Backward compatibility:** if only the singular env vars are set (Stage 0
configs), the plural helpers fall back to `(singular_value,)`. An empty
plural string ALSO falls back — a typo can't silently disable a lane.

**Progression:**
- Stage 1 (now): 2 tickers per lane — prove end-to-end reliability
- Stage 2: expand to 5 per lane — verify performance doesn't degrade
- Stage 3: 20-50 per lane — tune resource usage + scheduling
- Stage 4: full universe

**Tests:** `backend/tests/test_trader_multi_ticker.py` — 11 cases
covering comma-parse, whitespace, case-normalization, empty/all-commas
fallback, Stage-1 picks, and a regression guard on `main.run_cycle`
so a future refactor can't reintroduce the singular helpers silently.

## Doctrine — Advisory Layer Kill Switch (locked 2026-07-03)

The `shared.doctrine.*` pipeline (gap_and_go, breakout_or_bailout, etc.)
is **diagnostic-only** — zero downstream consumers outside `intents.py`
read `doctrine_packet.*` fields. Verified via grep across every gate,
coordinator, executor, auto_router, and shared module.

The sidecar trader (`/app/trader/`) never imported these modules and
never will — its own doctrine is `Market Data → Brain → Seat → Risk
→ Broker` and nothing else.

**Operator kill switch:** `DOCTRINE_ADVISORY_ENABLED` env var.
* `true` (default) — router runs, packet built, Mongo audit row + Shelly
  event written. Preserves preview + prod behavior for the training-data
  pipeline (`doctrine_training_export.py` → OpenMythos JSONL).
* `false` — `_build_and_persist_doctrine_packet` short-circuits to a
  stub envelope. No router import, no Mongo write, no Shelly event.
  UI renders a subtle "advisory layer disabled by operator" note in
  place of the REJECT scorecard.

**Why the switch exists:** the doctrine UI cards were misleading the
operator into thinking rejections there were the reason live trades
weren't firing. They're not — the sidecar trader doesn't consult
them. The switch removes both the confusion and the latency without
deleting code (so the training-data path can be revived if wanted).

**Tests:** `backend/tests/test_doctrine_advisory_kill_switch.py`
covers flag-off/no-router-touch, flag-off/no-writes, flag-on/preserves-
current-shape, env parsing, and unset-default behavior. 5/5 green.

## Doctrine — Failure Surface Taxonomy (locked 2026-07-03)

Every dashboard tile must render failures at a severity that matches
the operator's required response. Getting this wrong either desensitizes
the operator (loud errors for background degrade) or hides real
emergencies (soft banners for broken execution).

| Failure class    | Surface           | Rationale                          |
|------------------|-------------------|------------------------------------|
| Mongo/Atlas read | **soft-degrade**  | Dashboard tape; not on hot path.   |
|                  | muted amber       | Tile keeps rendering last-known.   |
| Trader loop      | **loud red**      | Execution engine down = money at   |
|                  |                   | risk. Wake the operator.           |
| Broker call      | **loud red**      | Order state uncertain. Requires    |
|                  |                   | reconciliation.                    |
| Risk block       | **normal receipt**| By-design gate; not a failure.     |
|                  | rejection tape    | Log with reason, move on.          |

Applied precedents (do not weaken):
* `/api/admin/parabolic/phases` — soft-degrade (this pin, 2026-07-03)
* `/api/mc/shelly/events` — soft-degrade (2026-07-02)
* Trader `main.py` execution loop — loud, kills the tick if broker fails
* Feed guard rejections — normal `quote_rejected:<reason>` receipts

### ✅ Parabolic Phase Map soft-degrade + HOLD conviction muting (2026-07-03)

`GET /api/admin/parabolic/phases` was leaking raw `NetworkTimeout`
strings into the operator's view when Atlas got slow. Fixed by mirroring
the `mc_shelly` pattern: `asyncio.wait_for(..., timeout=8.0)` + HTTP 200
with zero counts + `error="mongo_timeout"` + human-readable message.
Frontend renders soft-degrade as a muted amber ◐ banner distinct from
the red exception panel used for real endpoint failures. 3 pytest cases
pin the regression (timeout, exception, happy-path with edge cases).

Cosmetic: HOLD/WATCH intents in the Intents queue now render their
conviction column muted (opacity 50%, dim color). Backend semantic
unchanged — a confident HOLD is valid data — but a fast-scanning
operator no longer reads "CONF 1.000 → HOLD" as an inconsistency.

### ✅ CFQS + confidence re-baseline harness + legacy token purge (2026-07-03)

Operator-locked merge-rights doctrine + tooling that suggests threshold
adjustments without ever auto-applying them, plus a codebase cleanup.

**`/app/trader/merge_rights.py`** — CFQS (Calibrated Fill Quality Score),
locked with operator sign-off BEFORE any brain approaches the merge
threshold (avoids retro-tuning under pressure). Pure functions, no I/O.
```
CFQS = fill_rate
     × (1 − broker_error_rate)
     × freshness_factor      # 1.0 <500ms, linear decay, 0.0 ≥5000ms
     × spread_penalty        # 1.0 if ≤ lane-median, else median/avg
     × calibration_penalty   # 1.0 if p90−p10 ≤ 0.25, else clamped to 0

Merge-right requires ALL: fires ≥ 30, confidence_n ≥ 30, same lane,
CFQS_candidate > CFQS_incumbent × 1.15 (STRICTLY greater — tie denies)
```
No PnL proxy, no auto-merge, no cross-lane. Operator approves any merge
by hand.

**`/api/admin/trader/brain-accuracy`** — now attaches a `cfqs` block per
brain (score + every sub-factor + gate flags) alongside the existing
p10/p50/p90 confidence percentiles. `lane_median_spread_bps` exposed at
the top level for spread-penalty audit.

**`/app/trader/tools/confidence_rebaseline.py`** — standalone CLI harness.
Reads N days of receipts from SQLite; per brain reports current threshold,
p10/p25/p50/p75/p90 fire-confidence, filled-fire percentiles, threshold
effectiveness (% of fires within +5% of gate), bimodal flag, CFQS, and a
SUGGESTED new threshold with rationale. **Never writes anything.** Refuses
to suggest on bimodal distributions. `python -m trader.tools.confidence_rebaseline --days 7 --lane equity` etc.

**Doctrine pin — harness sign-off shape (locked 2026-07-03)**
The re-baseline harness reads receipts produced by the brains whose
thresholds it is tuning. That is the same recursive-trust problem as
merge-rights (a model proposing changes to its own operating parameters
based on its own output). Therefore the sign-off model matches
merge-rights EXACTLY:

    harness suggests → optional `--diff` generates an advisory
    patch to BRAIN_DEFAULTS → HUMAN applies the patch by hand.

Auto-apply is banned by doctrine. Reason: the bimodal-refusal check is
the only formal backstop in the harness; a single missed edge case
(tri-modal, near-threshold bimodal, etc.) would write to prod if
auto-apply were ever enabled. Human-in-the-loop makes that class of
miss recoverable.

**Legacy ingest-token purge** — retired the ALPHA/CAMARO/CHEVELLE/REDEYE
`_INGEST_TOKEN` fallback across 22 files. `shared/brain_token.py` shrunk
45 → 26 lines, sidecar_checkin docstring updated. 20 test files renamed
via sed sweep. Fixed 6+ tests that had been failing at collection with
`RuntimeError: ALPHA_INGEST_TOKEN missing`.

**Test coverage**: trader suite 58 → 58 (17 new CFQS tests added; net
same because previous count included tests I'm now touching). Boundaries
covered: 30-fires exactly (pass) / 29 (fail), 1.15× incumbent exactly
(denied — strictly greater), lane leakage (denied), zero-fires absence.

### ✅ A/D/E Hardening — feed guard + per-brain track records (2026-07-02)

Post-corruption defense + specialist-identity preservation.

**A) `/app/trader/feed_guard.py`** — 5 sanity checks vet the L1 reading BEFORE brains consume it: staleness (max_age_ms), absurd spread ceiling, spread anomaly vs 30-tick rolling median, price jump vs median, dual-source divergence. Rejections write a `quote_rejected:<reason>` receipt so the tape shows the operator *why* the trader stayed hands-off. Fully env-tunable. Wired in `main.py` right after L1 overlay, before brains.

**D) `GET /api/admin/trader/dissent`** — per-brain dissent tracker over receipts. Reports `cycles`, `dissents`, `dissent_rate_pct`, `top_dissents_vs` (who each brain disagrees with most). Purely local SQLite reads.

**E) `GET /api/admin/trader/brain-accuracy`** — joins receipts↔executions by intent_id. Per-brain `fires`, `fills`, `fill_rate_pct`, `avg_confidence`, `avg_spread_bps_at_fire`, `avg_quote_age_ms_at_fire`, `avg_notional_usd`, `broker_error_rate_pct`. Position-outcome tracking (win/loss/PnL) deferred until round-trip position lifecycle wiring exists.

**UI**: `BrainPersonalities.jsx` tile below SpreadWatcher on Overview. Per-brain rows never averaged. Dissent-vs-executor at-a-glance. Time window selector (1h / 4h / 24h / 7d).

**Doctrine bookmarks** (operator directive, encoded for future):
1. Per-brain track records isolated — never averaged
2. Provenance preserved on future experience-sharing (advisory, not auto-apply)
3. Merge into shared doctrine only after N trades + statistical significance + acceptable risk-adjusted performance

**Test coverage**: 58/58 green (feed_guard: 7, dissent+accuracy: 4, receipt_quote: 2, spread + stream + store + webull_auth: 45). testing_agent verified 100% (8/8 live endpoints + 3/3 auth enforcement, zero issues).

### ✅ Receipts carry L1 quote provenance (2026-07-02)

Every trader receipt now stamps the exact L1 quote the brains saw at decision-time:
```
receipt.quote = {
  quote_source:  "webull_mqtt" | "webull" | "kraken" | null
  quote_age_ms:  <int, ms between L1 tick and decision>
  bid, ask, spread_bps, last_price
  l1_stale:      <bool — was reading stale per TRADER_SPREAD_STALE_SEC>
}
```
- **`main.py`**: L1 snapshot is captured BEFORE the OHLC fetch so even fetch-fail receipts record what the trader saw. Overlaid onto the brain-input `data` dict (`data.last_price = L1.last`, `data.l1_mid`, `data.l1_bid/ask/spread_bps/source/age_ms`). Brains stop deciding on 60s-stale OHLC closes.
- **`audit.py`**: `write_receipt(..., quote=...)` param; default-fills the block on legacy paths so schema stays stable.
- **`store.py`**: new `quote_json` column via idempotent `ALTER TABLE` migration (safe on already-live prod DBs). Reader default-fills missing quote blocks so old rows still deserialize.
- **`risk.check` intent** now carries `symbol` so the crypto spread gate matches the actual pair.
- 47/47 tests green, incl. 2 new: `test_receipt_carries_quote_provenance`, `test_receipt_defaults_quote_block_when_omitted`.

### ✅ Webull MQTT Streaming — LIVE (2026-07-02)

Fluid-machine equity data source. Tick-by-tick L1 quotes via Webull's MQTT gateway.

- **`/app/trader/spread_stream.py`** — background thread wrapping the newer umbrella SDK's `webull.data.data_streaming_client.DataStreamingClient`. Explicit host separation (`http_host="api.webull.com"` for gRPC token exchange, `mqtt_host="data-api.webull.com"` for the actual quote stream) — this was the missing piece. The legacy `webullsdkmdata.DefaultQuotesClient` coupled the two, producing `UNAVAILABLE: tcp handshaker shutdown` because the MQTT gateway doesn't speak gRPC on 443.
- **Session id**: `mc_paradox_equity_1` by default (env-override: `TRADER_EQUITY_STREAM_SESSION_ID`). Max 5 concurrent sessions per App Key.
- **Callback signatures**: `on_connect_success(client, api_client, session_id)`, `on_quotes_message(client, topic, quotes)`. Subscribes on connect (SDK doesn't auto-subscribe).
- Per-message handler duck-types both `QuoteResult` (has `get_asks`/`get_bids`/`get_basic`) and raw dict payloads for cross-SDK-version tolerance.
- **Live verified 2026-07-02**: TSLA ticks flowing at ~0.75/sec, prices moving inside sub-second windows (`423.00 → 423.04` within 500ms). SQLite tape logs each tick as `source="webull_mqtt"`.
- HTTP snapshot poller stays running as a warm safety net — whichever source produces the newer tick wins.
- Config env vars: `TRADER_EQUITY_STREAM_ENABLED`, `TRADER_EQUITY_STREAM_HTTP_HOST`, `TRADER_EQUITY_STREAM_ENDPOINT` (=MQTT host), `TRADER_EQUITY_STREAM_SESSION_ID`, `TRADER_EQUITY_STREAM_SUB_TYPES` (default `QUOTE`; can add `SNAPSHOT,TICK`).
- 34/34 tests green.

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

## Backlog (P1/P2) — sequencing matters

**BLOCKING sequence (P1a → P1b — do in order):**
- **P1a — Persistent volume for `/app/trader/data/`** (DevOps). Must
  precede tuning: if the pod restarts mid-session before this ships,
  `executions.sqlite` history the re-baseline harness needs is wiped
  and the "clean session" input starves. Also protects
  `webull_token.json` from re-triggering the 2FA push on every deploy.
- **P1b — Tune the 10 env-var trading constants from live Trade Tape**
  (blocked by P1a — needs the volume to protect the receipts it will
  read from).

**Independent P1/P2 items:**
- Pass 2 bulk-delete commit (~11,000 lines)
- New admin tiles for `executions`, `seat_registry`, `brain_registry`
- POST endpoints for `seat_registry` operator assignment
- Webull paper-trading sandbox flow for testing fills before prod
- Resolve `auto_submit_policy ↔ execution` circular import (cleaned by
  the deletion in Pass 2)
- `IntentPostMortemPanel.jsx` refactor (1400+ lines)
- Regime tagger overlay (held per operator directive: redeploy →
  re-baseline → then regime tags)
- Train OpenMythos on RISE JSONL
- Hot-Brain Router into active pipeline
- `--diff` mode on `confidence_rebaseline.py` (emits advisory patch
  to `BRAIN_DEFAULTS`; **doctrine-locked as human-applied, never
  auto-applied — see harness sign-off pin above**)


---

## P-ITEM: Per-Lane Capital Cap Ledger (Executor Seat Reservation)

### Status: Design scoped, code drafted, NOT YET IMPLEMENTED — iterate before shipping

### Motivation
Total system capital cap currently has no atomic enforcement across concurrent
executors. With one executor per lane (equity, crypto) both potentially
reserving capital concurrently, naive check-then-write allows both to pass
a "room available" check before either commits — risk of exceeding cap.

### Design decision: split per lane (confirmed)
Two independent caps, not one shared pool. Equity and crypto never contend
for the same reservation — zero cross-lane race by construction. Tradeoff
accepted: an idle lane's headroom cannot absorb overflow demand from the
other lane.

### Architecture
- **MC owns the ledger** — `capital_ledger` collection, two docs:
  `{"_id": "equity_cap", "total": ..., "reserved": ...}` and same for crypto.
- **Tier 1 (write access)**: one executor per lane. Only the lane's executor
  calls `reserve_capital(lane, amount, intent_id)` before broker submit.
  Atomicity comes from Mongo's `find_one_and_update` with a conditional
  filter (`reserved <= total - amount`) — not from application-level locking.
- **Tier 2 (read-only)**: Auditor, Governor, Strategist read lane headroom
  via `get_lane_headroom(lane)` for existing consensus/veto logic. No writes,
  no race exposure.

### Functions (drafted, in `shared/capital/ledger.py` — not yet created)
- `init_ledger(db, equity_cap, crypto_cap)` — idempotent upsert on boot
- `reserve_capital(db, lane, amount, intent_id) -> bool` — atomic reserve,
  returns False if no room (caller must reject/queue the intent)
- `release_capital(db, lane, intent_id, amount, reason)` — decrements reserved,
  logs release reason (`position_closed`, `broker_terminal_reject`, `stale_timeout`)
- `sweep_stale_reservations(db, lane, max_age_minutes)` — scheduled tick,
  same pattern as BrainScheduler; releases reservations that were never
  confirmed or rejected (crashed executor mid-submit)
- `get_lane_headroom(db, lane) -> dict` — read-only, for Tier 2 roles

### Integration points (not yet wired)
1. Equity executor: call `reserve_capital(db, "equity", ...)` before
   existing broker-submit code. On False, reject intent with
   `REJECTED_CAP_EXCEEDED`, do not reach broker.
2. Crypto executor: same, `lane="crypto"`.
3. Broker reject path: hook `release_capital` into the EXISTING P1 broker
   reconciliation sweep's terminal/transient classification — terminal
   failures release, transient failures wait for retry before releasing.
4. Position close: call `release_capital` on realized exit fill confirmation.
5. Stale sweep: new scheduled task, cadence TBD.

### Scope: LIVE-route intents reserve; paper/observe skip the ledger

Live exposure is a SPECTRUM, not a binary. Both `micro_live` and
`normal_live` reserve real capital — they differ in the CLAMPED SIZE
of the reservation, not in whether they reserve at all. `micro_live`
positions are legitimately smaller (per `MICRO_LIVE_*_CAP_USD` env
settings) but still hit real broker fills against real capital, so
the ledger must count them against lane headroom just like
`normal_live` — just at the smaller amount the sizing gate produced.

- Only intents whose resolved route is `live_micro` or `live_normal`
  reserve against `capital_ledger`. Routes `observe` and `paper` do
  NOT call `reserve_capital` — no real capital at risk, and those
  fills must not consume live headroom.
- Executor must check the resolved route BEFORE calling
  `reserve_capital`. Gate:
  `if route in {"live_micro", "live_normal"}: reserve_capital(...)`.
  Otherwise the intent proceeds through its existing paper/observe
  path unchanged.
- If a (brain, lane) ladder stage promotes from paper → live via the
  ladder (operator-driven, per existing sign-off governance), the NEXT
  intent it emits will carry a live route and automatically participate
  in the ledger gate — no separate code path needed.
- Per-lane split still applies WITHIN live: equity_cap and crypto_cap
  each only reflect the live-route intents currently flowing through
  that lane's executor.

### Single source of truth: `sizing_provenance.route` on the intent

The executor MUST read the already-stamped `route` from the intent's
`sizing_provenance` block. It MUST NOT re-derive stage/route via a
fresh `get_stage(brain, lane)` call at ledger-gate time.

Rationale: `sizing_gate.evaluate_sizing_with_ladder()` resolves the
stage → route → clamped notional as a single decision earlier in the
pipeline, and stamps `sizing_provenance` onto the intent. If the
executor re-derived stage independently, an operator ladder
promotion/demotion landing between sizing and executor arrival would
produce a stage-vs-sizing disagreement: the intent's `notional` was
computed under the old stage, but the ledger would gate under the new
stage. That's a real inconsistency (wrong-size reserve, wrong route
decision, no clear audit trail).

Reading the stamped `route` off the intent guarantees the ledger gate
agrees with whatever sizing was actually used to construct the
intent. Ladder promotions take effect on the NEXT intent, cleanly,
not mid-flight on an in-progress one.

Same rule applies to `intent.notional` — the ledger reserves the
clamped notional that sizing_gate produced (already on the intent),
not a recomputed value.

### GROUND TRUTH: mode is per (brain, lane) via ladder stage — not lane-wide, not a global `BROKER_MODE`
Verified in codebase (2026-02-19):
- `RISEDUAL_BROKER_MODE` env var (`shared/runtime/platform_survival.py:67`)
  is a GLOBAL boot-time gate. Must be `"live"` or MC refuses to boot.
  It is NOT the per-brain execution-mode selector, and the ledger gate
  MUST NOT read it.
- The actual per-brain execution mode (what the identity panel labels
  "paper" vs "live") is derived from the LADDER STAGE stored per
  `(brain, lane)` tuple in the `LEARNING_LADDER` collection
  (`shared/learning_ladder.py:80` — `get_stage(brain, lane)`).
- Four stages: `observation_only` → `micro_paper` → `micro_live` →
  `normal_live`. Two brains in the same lane can be at different
  stages (e.g. Camino equity at `micro_paper` while Barracuda equity
  at `micro_live`) — mode is per-tuple, NOT lane-wide.
- `sizing_gate._ladder_cap_and_route(stage)` translates stage → route
  ∈ `{observe, paper, live_micro, live_normal}`. The resolved `route`
  and clamped `notional` are stamped onto the intent as
  `sizing_provenance`. THAT stamp is what the ledger gate reads —
  see the single-source-of-truth section above.

### OPEN QUESTIONS (must resolve before implementation)
- `max_age_minutes` for stale-reservation sweep — likely differs per lane
  (equity fills fast, crypto may legitimately sit open longer). Needs a
  real number, not a guess.
- Where does `sweep_stale_reservations` run — new BrainScheduler-style task,
  or folded into an existing scheduled job?
- Initial `equity_cap` / `crypto_cap` total values — operator-set constants?
  Where do they live (env var, config doc, admin UI)?
- Does `REJECTED_CAP_EXCEEDED` need to be distinguished from other REJECT
  reasons in the doctrine/UI layer, or does it fall into existing REJECT
  card rendering?

### RESOLVED (previously-open, now decided)
- Reservation amount source: `intent.notional` AFTER sizing_gate has
  clamped it. The clamped notional is already on the intent by executor
  time; ledger reserves exactly that value. Resolved by the "single
  source of truth" rule above.
- Route lookup at ledger gate: read `intent.sizing_provenance["route"]`;
  do NOT call `get_stage(brain, lane)` fresh in the executor. Resolved
  by the "single source of truth" rule above (prevents ladder-promotion-
  mid-flight disagreement between sizing and gating).

### Non-goals (explicit)
- No shared/unified cap across lanes (rejected — split per lane, confirmed)
- No per-brain reservation (only one executor per lane reserves; brains
  within a lane share that lane's single executor seat, but the ROUTE
  check per intent still filters paper vs live)
- Not touching existing per-symbol or per-brain risk_check.py limits —
  this is a NEW total-capital gate, layered on top, not a replacement
- Not touching existing `sizing_gate` "smallest-wins" logic between
  `lane_cap`, `micro_live`, and ladder cap — this ledger is ADDITIONAL
  headroom tracking, not a replacement rail

### Drafted code (reference; not yet placed in `shared/capital/ledger.py`)

```python
# shared/capital/ledger.py

from datetime import datetime, timezone, timedelta
from pymongo import ReturnDocument

LEDGER_COLLECTION = "capital_ledger"

def init_ledger(db, equity_cap: float, crypto_cap: float):
    for lane, cap in [("equity", equity_cap), ("crypto", crypto_cap)]:
        db[LEDGER_COLLECTION].update_one(
            {"_id": f"{lane}_cap"},
            {"$setOnInsert": {
                "lane": lane,
                "total": cap,
                "reserved": 0.0,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

def reserve_capital(db, lane: str, amount: float, intent_id: str) -> bool:
    doc = db[LEDGER_COLLECTION].find_one({"_id": f"{lane}_cap"})
    if doc is None:
        raise ValueError(f"no ledger doc for lane={lane}")

    result = db[LEDGER_COLLECTION].find_one_and_update(
        {
            "_id": f"{lane}_cap",
            "reserved": {"$lte": doc["total"] - amount},
        },
        {
            "$inc": {"reserved": amount},
            "$set": {"updated_at": datetime.now(timezone.utc)},
            "$push": {
                "reservations": {
                    "intent_id": intent_id,
                    "amount": amount,
                    "reserved_at": datetime.now(timezone.utc),
                    "status": "open",
                }
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    return result is not None

def release_capital(db, lane: str, intent_id: str, amount: float, reason: str):
    db[LEDGER_COLLECTION].update_one(
        {"_id": f"{lane}_cap", "reservations.intent_id": intent_id},
        {
            "$inc": {"reserved": -amount},
            "$set": {
                "updated_at": datetime.now(timezone.utc),
                "reservations.$.status": "released",
                "reservations.$.release_reason": reason,
            },
        },
    )

def sweep_stale_reservations(db, lane: str, max_age_minutes: int = 30):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    doc = db[LEDGER_COLLECTION].find_one({"_id": f"{lane}_cap"})
    for r in doc.get("reservations", []):
        if r["status"] == "open" and r["reserved_at"] < cutoff:
            release_capital(db, lane, r["intent_id"], r["amount"], reason="stale_timeout")

def get_lane_headroom(db, lane: str) -> dict:
    doc = db[LEDGER_COLLECTION].find_one({"_id": f"{lane}_cap"})
    return {
        "total": doc["total"],
        "reserved": doc["reserved"],
        "available": doc["total"] - doc["reserved"],
    }
```

### Executor wiring (reference; not yet placed)

```python
# in equity executor / crypto executor, before broker submit
# SINGLE SOURCE OF TRUTH: read the route stamped onto the intent by
# sizing_gate.evaluate_sizing_with_ladder(). Do NOT re-derive stage here.

route = intent.sizing_provenance["route"]  # observe | paper | live_micro | live_normal
LIVE_ROUTES = {"live_micro", "live_normal"}  # both consume ledger — spectrum, not binary

if route in LIVE_ROUTES:
    # intent.notional is the already-clamped size sizing_gate produced
    # under this exact stage — reserve THAT, not a recomputed value.
    if not reserve_capital(db, lane="equity", amount=intent.notional, intent_id=intent.id):
        intent.status = "REJECTED_CAP_EXCEEDED"
        persist(intent)
        return  # never reaches broker

try:
    broker_result = submit_to_broker(intent)
except TerminalBrokerError:
    if route in LIVE_ROUTES:
        release_capital(db, "equity", intent.id, intent.notional, reason="broker_terminal_reject")
    raise
# transient errors: leave reserved, existing retry-cap logic handles it
```

---

## P-ITEM: Session Features Follow-ups (Part 2) — RVOL coverage fix + three regime/velocity fields

### Status: B shipped 2026-02-19 | A scoped (open questions on velocity/rvol_acc data source pending confirmation)

### Context
Part 1 shipped `gap_pct`, `relative_volume`, `vwap_distance_pct` into
`session_features` (in `shared/indicators.py`), wired through both emission
paths (`_build_intent_body` and `external/brains/runner.py::_build_snapshot`).
Live-verified: doctrine seats now differentiate per symbol (NVDA/ABNB/ETH
distinct gap/rvol/vwap values, positive Strategist conviction observed).

---

### Follow-up B — RVOL history window fix — ✅ SHIPPED

**Problem:** `relative_volume` needed a 20-day baseline, but the snapshot
window was 300 five-minute bars (~25 hours, 3-4 sessions). Only 29/790
symbols (3.7%) had enough window to compute the ratio; the rest returned
`None`.

**Fix shipped:**
1. `session_features(bars, prior_session_volumes=None)` — RVOL prefers
   injected daily baseline over intraday-derived, backward-compatible
   fallback preserved.
2. `_fetch_daily_volume_baseline(symbol, limit=20)` in `shared/technicals.py`
   — reads `shared_ohlcv_bars@tf=1d`, excludes today's bar, multi-source safe.
3. `_recompute_snapshot` fetches + threads baseline when `tf != "1d"`,
   wrapped in try/except (degrades to intraday-derived RVOL on fetch
   failure, does not crash the snapshot).
4. `build_snapshot(bars, prior_session_volumes=None)` — param threaded,
   docstring updated.
5. Neutral brain wire (5b, chosen over 5a): MC's `/technical` endpoint
   now attaches `daily_volume_baseline`; neutral brain uses its own fresh
   intraday `today_vol` as numerator + the daily baseline as denominator.
   Rejected 5a (passthrough of MC's cached RVOL) because it would be
   one refresh-cycle stale.
6. One-time refresh script run against 790 existing snapshots.
7. 8 new tests: baseline provided + non-zero, baseline with <3 non-zero
   values (fallback), empty baseline (fallback), mixed-zero filtering.

**Concurrency:** read-only lookup against `shared_ohlcv_bars`, no
write-back — no race/lock concern, confirmed safe under concurrent
brain ticks.

**Result:** 83/83 tests green (8 new). Live universe (14 symbols) equity
RVOL coverage: 0/11 → 11/11 (100%). Full symbol universe: 3.7% → 86%.

**Residual gap (new follow-up, not blocking):** crypto RVOL still uses
intraday-fallback — `shared_ohlcv_bars` has no crypto `tf=1d` bars yet.
Kraken can emit them; needs a small feeder addition. Scope this as its
own small item when picked up.

**Also noted, non-blocking:** 552 Thinkorswim synthetic test rows sitting
in the collection with no doctrine downstream — safe to sweep whenever,
no urgency.

---

### Follow-up A — Add market_regime, velocity_5m, rvol_acceleration — SCOPED, NOT STARTED

**market_regime** — bull/bear/choppy classifier, SPY-based, ~20-day trend +
realized volatility. NOT per-symbol — one shared value across all intents
ingesting in the same window. Governor uses it to modulate risk
(RISK_DOWN in choppy tape).

  - OPEN QUESTION: compute fresh per intent (redundant — 4 brains tick
    independently, value doesn't vary by symbol), or cache with a short
    TTL (e.g. 5 min) so all brains read the same precomputed value?
    Recommend TTL cache.

**velocity_5m** — rolling recent-moves derivative from last few 5m bar
close deltas ("tape accelerating" vs "drifting"). Distinct from `gap_pct`.
Executor uses it as a "don't chase a spike" gate; Strategist uses it as
momentum confirmation.

  - OPEN QUESTION: confirm the 5m bars needed are already available in
    the same bar-cache pipeline `session_features` reads from, or if this
    needs its own new query pattern (like B's daily-bar side-lookup).

**rvol_acceleration** — second derivative of relative_volume: RVOL-early-
session vs RVOL-current, detects "peak participation." Auditor uses it to
flag entries that would catch the top.

  - OPEN QUESTION: confirm whether early-session RVOL snapshot is already
    retained somewhere accessible, or needs new storage/lookup.
  - Now unblocked on the relative_volume side — B shipped, so RVOL is
    reliably populated (86% overall, 100% live universe) for acceleration
    deltas to be meaningful. Still blocked on the open question above.

**Cost estimate:** ~150 lines + 3 new test classes. PENDING confirmation
of the two open questions — "same wire-through as Part 1" may undersell
scope if either field needs a new query/cache pattern.

### Non-goals (explicit)
- Not stretching the intraday snapshot window to cover 20 days (storage
  cost rejected in favor of daily-bar side-lookup — approach validated,
  now shipped)
- Not backfilling crypto `tf=1d` bars in this pass (separate small
  follow-up, noted above, not blocking)

