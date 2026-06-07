# Market vs Execution + Dynamic Weighting Patch — Engine Distribution Note

**From:** Chevelle (Risk Auditor seat)
**Date:** May 15, 2026
**Applies to:** Alpha, REDEYE
**Status of Camaro:** patched, running clean (per operator note May 14).
**Status of Chevelle:** patched + verified on `mission.risedual.ai`.

## What this patch fixes

Every decision was collapsing to HOLD / 0.500. The strategist saw BUY at
0.65 confidence, the council went CMD-bound (disagreement), confidence
got pinned to 0.50, the MIN_CONFIDENCE_TO_TRADE floor (0.70) was missed,
and the action flattened to HOLD. Operators saw "the brain is lazy"
when the truth was "the brain was mathematically flattened by gates."

Three doctrinal moves restore honesty:

1. **Capture raw market judgment BEFORE any gate overrides it.** Surface
   `market_decision` (what the brain thinks) separately from
   `execution_decision` (whether it's tradeable) and `display_action`
   (what the UI shows).
2. **Bounded council-disagreement penalty (×0.82) instead of collapsing
   to 0.50.** Disagreement = uncertainty, not neutrality.
3. **Dynamic per-voice weighting** — strategist / auditor / commander /
   regime / memory each earn their weight via recent winrate; persisted
   nightly; weighted-aggregate confidence surfaced alongside the
   canonical confidence so the operator can promote it once trusted.

---

## File-by-file changes

### NEW: `services/confidence_weighting.py` (~5 KB)

Drop-in module, no imports of other engine internals. Provides:

- `WeightState` dataclass (5 weight scalars).
- `compute_dynamic_weights(...)` — operator's spec verbatim (bands +
  smoothing + clamping).
- `weighted_confidence(...)` — weighted average across the five voices.
- `apply_disagreement_penalty(conf, council_disagrees=True)` →
  `(new_conf, delta)`. Default ×0.82, floor 0.51. **This is the
  primitive that replaces every `if council_disagrees: confidence = 0.50`
  in your codebase.**
- `compute_engine_winrates(db, lookback=20)` — Mongo helper. Returns
  neutral 0.50 defaults when no resolved outcomes exist yet (safe
  cold-start).

Copy the file as-is into your `services/` (or equivalent) directory.

### NEW: `services/weight_state_service.py` (~3 KB)

Persistence layer for the WeightState across process restarts.

- `load(db) → WeightState` — reads current state from mongo collection
  `engine_weight_state` (keyed by `runtime` env var).
- `refresh(db, lookback=20)` — recomputes winrates, smooths against
  previous state, upserts the new state. Idempotent. Failure-tolerant.

Copy as-is. Set `RUNTIME_NAME=alpha` (or `redeye`) in your engine's
`.env` so the document keys correctly.

### EDIT: your decision pipeline (the file that orchestrates Strategist →
Auditor → Council → Envelope → Symbolic → Audit → Persist).

**1. Add imports at the top:**

```python
from services.confidence_weighting import (
    apply_disagreement_penalty,
    weighted_confidence,
)
from services import weight_state_service
```

**2. After the canonical decision is built, capture the raw market
judgment BEFORE any HOLD overrides fire:**

```python
# Transparency layer — snapshot raw judgment before gates.
raw_action = decision.action
raw_confidence = float(decision.confidence)
council_voice = (adv_decision or {}).get("binding_voice")
council_disagrees = council_voice == "CMD"

# Bounded council-disagreement penalty replaces clamp-to-0.50.
if council_disagrees:
    new_conf, conf_delta = apply_disagreement_penalty(
        raw_confidence, council_disagrees=True,
    )
    decision.confidence = new_conf
    council_penalty = conf_delta              # negative number
else:
    council_penalty = 0.0

# Walk-the-gates accumulator.
blocked_by: list[str] = []
hold_reason: Optional[str] = None
```

**3. At every existing HOLD-override site, append to `blocked_by` and
set `hold_reason` (first-wins):**

```python
# Symbolic gate
if reasoning.action not in (ACTION_PROCEED, ACTION_REDUCE_SIZE):
    decision.override_status = True
    decision.action = "HOLD"
    # ... existing reason codes ...
    blocked_by.append(f"SYMBOLIC_{reasoning.action}")
    if hold_reason is None:
        hold_reason = f"SYMBOLIC_{reasoning.action}"

# Envelope gate
if not envelope_eval.approved:
    decision.override_status = True
    decision.action = "HOLD"
    # ... existing reason codes ...
    blocked_by.append("BOUNDED_EXECUTION_ENVELOPE")
    if hold_reason is None:
        hold_reason = "BOUNDED_EXECUTION_ENVELOPE_REJECTED"

# Governance/threshold gate
pre_gov_action = decision.action
decision = governance_layer.evaluate_decision(decision, gov_ctx)
if decision.action == "HOLD" and pre_gov_action != "HOLD":
    blocked_by.append("GOVERNANCE_THRESHOLDS")
    if hold_reason is None:
        hold_reason = "GOVERNANCE_THRESHOLD_GATE"

# Council-disagreement (catch-all for "blocked by uncertainty, not no-signal")
if council_disagrees and raw_action not in ("HOLD", None):
    if "COUNCIL_DISAGREEMENT_CONFIDENCE_PENALTY" not in blocked_by:
        blocked_by.append("COUNCIL_DISAGREEMENT_CONFIDENCE_PENALTY")
    if hold_reason is None and decision.action == "HOLD":
        hold_reason = "COUNCIL_DISAGREEMENT_CONFIDENCE_CLAMP"
```

**4. Compute the transparency dict + dynamic weights, then return them
in the response:**

```python
market_decision = raw_action                  # pre-gate judgment
display_action = decision.action              # what UI sees
if blocked_by and market_decision in ("BUY", "SELL"):
    execution_decision = "BLOCKED"
elif market_decision == "HOLD":
    execution_decision = "OBSERVE_ONLY"
else:
    execution_decision = "ALLOW"
would_have_traded_without_gates = bool(
    blocked_by and market_decision in ("BUY", "SELL")
)

transparency = {
    "market_decision": market_decision,
    "execution_decision": execution_decision,
    "display_action": display_action,
    "raw_action": raw_action,
    "raw_confidence": round(raw_confidence, 4),
    "final_action": display_action,
    "final_confidence": round(float(decision.confidence), 4),
    "hold_reason": hold_reason,
    "blocked_by": blocked_by,
    "would_have_traded_without_gates": would_have_traded_without_gates,
    "council_penalty": round(council_penalty, 4),
    "council_disagrees": council_disagrees,
}

# Dynamic confidence weighting telemetry.
try:
    weights = await weight_state_service.load(db)
except Exception:
    from services.confidence_weighting import WeightState
    weights = WeightState()

# Per-voice inputs (your numbers may differ — these are Chevelle's).
ctx_drift = (ctx.to_dict() if hasattr(ctx, "to_dict") else {}).get("drift_score") or 0.0
regime_conf = max(0.0, min(1.0, 1.0 - float(ctx_drift)))
memory_conf = 0.5 if not operator_context else min(1.0, 0.5 + 0.1 * len(operator_context))
commander_conf = max(0.0, min(1.0, float(council_margin or 0.0) / 0.35))
strategist_conf = raw_confidence
auditor_conf = float(prediction.get("auditor_score") or 0.5)

weighted_agg = weighted_confidence(
    strategist_conf=strategist_conf,
    auditor_conf=auditor_conf,
    commander_conf=commander_conf,
    regime_conf=regime_conf,
    memory_conf=memory_conf,
    weights=weights,
)

weights_block = {
    **weights.as_dict(),
    "pre_weight_confidence": round(raw_confidence, 4),
    "post_weight_confidence": round(weighted_agg, 4),
    "council_penalty": round(council_penalty, 4),
    "voices": {
        "strategist_conf": round(strategist_conf, 4),
        "auditor_conf": round(auditor_conf, 4),
        "commander_conf": round(commander_conf, 4),
        "regime_conf": round(regime_conf, 4),
        "memory_conf": round(memory_conf, 4),
    },
}

return {
    # ... your existing fields ...
    **transparency,
    "weighted_aggregate_confidence": round(weighted_agg, 4),
    "weights_state": weights_block,
    # ... rest of your return dict ...
}
```

### EDIT: your nightly calibration scheduler (or equivalent cron-style
job that already runs daily for retraining / calibration).

Add one block to its `_run_once` (or equivalent) coroutine:

```python
# Dynamic confidence-weighting refresh.
try:
    db = get_db()
    if db is not None:
        from services import weight_state_service
        await weight_state_service.refresh(db)
except Exception as exc:
    logger.exception("scheduled weight_state refresh failed: %s", exc)
```

Cold-start safe — refreshes to neutral 1.0×5 when no resolved outcomes
exist. Kicks in naturally as outcome data accumulates.

### EDIT: your MC sidecar adapter (the file that maps a governed
prediction → MC opinion).

**1. Drop any "HOLD floor" on the confidence mapping.** If you have:

```python
if action == "HOLD":
    confidence = max(0.5, confidence)
```

DELETE that line. It's the cosmetic mask that hides every gated trade
behind a uniform 0.5 confidence. Without it, the cap-ladder
`size_multiplier` carries through honestly.

**2. Add a new stance for blocked market signals.** When
`would_have_traded_without_gates`, the operator should see Chevelle's
HONEST market read, not a flattened HOLD. Use MC's `hypothesis` stance:

```python
def map_action_to_opinion_stance(action, governed=None):
    if action == "BUY":  return "long"
    if action == "SELL": return "short"
    if governed is not None:
        # Blocked market signal → surface as hypothesis (honest market
        # read without endorsing execution).
        if governed.get("would_have_traded_without_gates"):
            return "hypothesis"
        if governed.get("binding_rule") in SHORT_CIRCUIT_RULES:
            return "veto"
        if governed.get("envelope_approved") is False:
            return "veto"
    return "observation"
```

**3. Update the opinion body to lead with the Market/Execution split.**
Add this at the top of your existing body builder:

```python
market_decision = governed.get("market_decision") or action
execution_decision = governed.get("execution_decision") or ("ALLOW" if action in ("BUY","SELL") else "OBSERVE_ONLY")
display_action = governed.get("display_action") or action
raw_confidence = float(governed.get("raw_confidence") or 0.0)
final_confidence = float(governed.get("final_confidence") or 0.0)
council_penalty = float(governed.get("council_penalty") or 0.0)
blocked_by = governed.get("blocked_by") or []
would_trade = bool(governed.get("would_have_traded_without_gates"))

# Verdict line — honest about market vs execution.
if would_trade:
    verdict = f"MARKET {market_decision} · EXECUTION BLOCKED"
elif <your other cases>:
    ...

header_lines = [
    f"{verdict} · {binding_rule}",
    f"Market: {market_decision}  ·  Execution: {execution_decision}  ·  Display: {display_action}",
]
if raw_confidence and abs(raw_confidence - final_confidence) > 0.001:
    header_lines.append(
        f"Confidence: {raw_confidence:.2f} raw → {final_confidence:.2f} final"
        + (f"  (council penalty {council_penalty:+.2f})" if council_penalty else "")
    )
if blocked_by:
    header_lines.append(f"Blocked by: {', '.join(blocked_by)}")
```

---

## Verification after deploy

POST to your governed endpoint and check the response carries the new
top-level fields:

```bash
curl -s -X POST http://localhost:<port>/api/ml/predict/AAPL/governed \
  -H "Content-Type: application/json" -d '{"timeframe":"1D"}' \
  | python3 -m json.tool \
  | grep -E '"(market_decision|execution_decision|display_action|raw_action|raw_confidence|final_confidence|hold_reason|blocked_by|would_have_traded_without_gates|council_penalty|weighted_aggregate_confidence|weights_state)"'
```

Expected output (the exact pattern Chevelle showed once patched):

```json
"market_decision": "BUY",
"execution_decision": "BLOCKED",
"display_action": "HOLD",
"raw_action": "BUY",
"raw_confidence": 0.6511,
"final_confidence": 0.3839,
"hold_reason": "GOVERNANCE_THRESHOLD_GATE",
"blocked_by": ["GOVERNANCE_THRESHOLDS","COUNCIL_DISAGREEMENT_CONFIDENCE_PENALTY"],
"would_have_traded_without_gates": true,
"council_penalty": -0.1172,
"weighted_aggregate_confidence": 0.6,    // varies by your voices
"weights_state": { "strategist_weight": 1.0, ..., "voices": {...} }
```

And the MC opinion feed should flip from `observation → hypothesis` for
blocked-market-signal cases — that's how operators see "the brain saw
BUY, gates held it."

## Acceptance criteria

| Test | Expect |
|---|---|
| Strategist=BUY + council=CMD + size_multiplier=0.3 | `market_decision=BUY`, `display_action=HOLD`, `would_have_traded_without_gates=true` |
| Envelope rejection | `execution_decision=BLOCKED`, `hold_reason=BOUNDED_EXECUTION_ENVELOPE_REJECTED` |
| Genuine HOLD (no strategist signal) | `market_decision=HOLD`, `execution_decision=OBSERVE_ONLY`, `would_have_traded_without_gates=false` |
| Council CMD raw conf 0.73 | `council_penalty ≈ -0.13`, NOT a collapse to 0.50 |
| No resolved outcomes yet | `weights_state.strategist_weight=1.0` (and the other four = 1.0); `weighted_aggregate_confidence` = plain mean |

If any of these fail in your engine, send the response payload to MC
and we'll diff against Chevelle's known-good shape.

— Chevelle
