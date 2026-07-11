# Camino Runner Audit Checklist

**Source of truth**: `/app/external/brains/runner.py` (Camino-specific paths) + `/app/external/brains/brain_core.py` + `/app/external/brains/personality.py`.

**Purpose**: Capture every implicit contract hidden in Camino's runner BEFORE the runner is deleted (migration step 8). Each row must be `[x] Captured in MC or brain class` before step 7 fires.

---

## Contracts

| # | Contract | Where in runner | Where captured in MC / brain |
|---|---|---|---|
| 1 | **Brain id normalization**: `"camino"` display → `"alpha"` slot code | `personality.py::BRAIN_PERSONALITIES` | `mc_brains/camino.py::CaminoBrain.id = "camino"` (we use display name at the pulse layer; personality module still resolves internally) |
| 2 | **Personality confidence multiplier** ×1.00 (balanced) | `personality.py::apply_personality_confidence` | `mc_brains/camino.py` calls `apply_personality_confidence("alpha", raw)` before returning the opinion — same audit evidence stamped on the opinion's rationale |
| 3 | **Min confidence floor** (`min_commitment`, default 0.58, doctrine-overridable) | `brain_core.py::evaluate` | Preserved inside `NeutralAdversarialBrain.evaluate()` which CaminoBrain wraps. If MC's arbiter is the new gate, the brain still returns `direction=HOLD` below floor — that's honest, not a soft gate. DAWE will grade HOLDs. |
| 4 | **Min gap between top-2 hypotheses** (0.06 equity / 0.03 crypto) | `brain_core.py::evaluate` | Preserved inside `NeutralAdversarialBrain.evaluate()`. Same rationale as #3. |
| 5 | **Operator threshold override cache** (`brain_tuning_cache`) | `brain_core.py::evaluate` inline import | Preserved — CaminoBrain does not shortcut this. Operator UI overrides continue to work through migration. |
| 6 | **Market quality score** (OBSERVE hypothesis surfaces as separate `market_quality_score` field, NOT a direction) | `brain_core.py::evaluate` | Passed through into the ModelOpinion's `rationale` string until Phase 2 wires a proper field. Doctrine holds: OBSERVE is not a direction. |
| 7 | **Position-context injection** (current_side / signed_qty / allowed_transitions) | `runner.py::_run_tick` fetches from `shared_positions` and hands to `evaluate` | **MC's responsibility.** `SnapshotService.build_all` will add `position_context` to the snapshot's `indicators` mapping or a dedicated field in Phase 2. **Step 2 v0.1: brains receive `position_context=None`** — Camino's evaluate falls back to FLAT semantics, which was already the runner's default when no position existed. Positions rejoin the snapshot in step 3 refinement. |
| 8 | **Symbol universe / cadence** — Camino ticks on the runner's own 30s cadence, iterating a symbol list from `SYMBOLS_ALPHA` env or default | `runner.py` | **MC's responsibility.** Pulse ticks at 15s; CaminoBrain sets `cadence_seconds=30` + `should_evaluate` returns True only every other pulse. `SnapshotService` owns the universe (starts with the same env-driven list, refactored to `MC_UNIVERSE_EQUITY` / `MC_UNIVERSE_CRYPTO` in Phase 2). |
| 9 | **Freshness rejection** — quotes older than N seconds get skipped | `runner.py` | **MC's responsibility.** `SnapshotService.build_all` filters bars older than 5 min per symbol; Camino trusts what MC hands it. |
| 10 | **Evidence stamping** — what goes into the intent's `evidence` dict | `runner.py::_emit_intent` | Adapted: CaminoBrain returns a rationale string; the pulse wraps it in an `OpinionEnvelope` with `pulse_id`/`snapshot_id`/`seat_key`. Backwards-compat evidence is added in step 6 when arbiter takes pulse envelopes. |
| 11 | **Deduplication** — same-tick same-symbol re-emission guards | `runner.py::_seen_this_tick` | Handled by pulse's idempotency contract: unique `(pulse_id, brain, symbol, lane)` on `mc_seats` and `mc_opinions_compare`. |
| 12 | **Heartbeat + sidecar check-in** | `runner.py::_send_heartbeat`, `sidecar_checkin.py` | Handled by `PulseReceipt` — pulse IS the heartbeat. Runner-side heartbeats remain until step 8 (grep-verified deletion). |
| 13 | **Runtime mode** (DISARMED / LIVE / OBSERVATION) | `runner.py::_should_route_to_broker`, `shadow_only` flag | Owned by arbiter (`brain_runtime_metrics.risedual_stack.arbiter.runtime_mode`). Camino evaluates regardless of mode; arbiter decides emission. |
| 14 | **Doctrine binding** (Camino → trend follower) | `brain_core.py::BrainIntent.doctrine` | Stamped on the `ModelOpinion.rationale` string. Full doctrine field in Phase 2. |

---

## Checklist for step 6 (audit close-out)

- [ ] Every contract row 1–14 has a live "captured in" entry
- [ ] Camino evaluates on both paths (runner + pulse) for ≥1 full session
- [ ] Parity check on `mc_opinions_compare` vs runner-emitted `shared_intents`: action-rate match ≥ 90%, confidence distribution overlap ≥ 80%
- [ ] No orphan contracts discovered during the parity comparison window (any surprise divergence surfaces a hidden contract that wasn't captured)

Once all four boxes tick, step 7 (`supervisorctl stop camino_runner`) is safe.
