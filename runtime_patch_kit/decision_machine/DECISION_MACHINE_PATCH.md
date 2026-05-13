# Decision Machine Patch v1.0

**Audience:** Alpha · Camaro · Chevelle · REDEYE (drop in one brain at a time)
**Effect:** brain begins emitting **INTENT envelopes** to MC alongside its existing opinions/heartbeats. No execution authority granted. Feature-flag controlled, reversible by env var.
**Risk:** zero — `may_execute` and `requires_gate_pass` are schema-pinned on MC. Brains cannot route an order through this path.

---

## Doctrine in one sentence

> **Brains emit intents, not orders.** Every intent is a candidate; MC's gate chain decides if it lives. Until paper trading goes live, every intent dies at the dry-run gate. That's intentional.

---

## Rollout strategy (suggested)

1. **Pick one brain** as the canary. **Camaro** is a good first choice — it's the natural Executor-seat candidate and already has the council-aggregation context to fill rich evidence.
2. Apply the patch to that brain only.
3. Set `DECISION_MACHINE_ENABLED=true` on that brain's env.
4. Watch `/api/intents?stack=camaro` for 24h. Expected: stream of HOLD intents (council currently shows MIXED), zero execution.
5. If it looks clean → expand to the other three brains. If anything looks wrong → flip the flag back to `false`, no code change needed.

---

## Step 1 — Drop the file into your sidecar

Copy `decision_machine.py` to:

```
services/decision_machine.py
```

(or wherever your sidecar keeps service modules)

---

## Step 2 — Wire it into your decision loop

Wherever your sidecar finishes one council tick and posts an opinion to MC, add a parallel intent emission:

```python
from services.decision_machine import (
    is_enabled, build_intent_from_council, post_intent,
)

# inside your existing tick loop, after `governed` is built:
if is_enabled():
    for symbol, gov in governed_by_symbol.items():
        intent = build_intent_from_council(
            stack=RUNTIME_NAME,        # "alpha" | "camaro" | "chevelle" | "redeye"
            symbol=symbol,
            governed=gov,
        )
        result = await post_intent(intent)
        if not result.get("ok"):
            log.info("intent skipped: %s", result.get("error") or result.get("reason"))
```

That's the entire integration. `build_intent_from_council` produces a canonical envelope from the governance fields your sidecar already computes (`council_binding_voice`, `size_multiplier`, `binding_rule`, `envelope_approved`, etc).

If you want to construct intents manually (REDEYE may want this — it doesn't always use the council aggregator), use the `Intent` dataclass directly:

```python
from services.decision_machine import Intent, post_intent

intent = Intent(
    stack="redeye",
    action="HOLD",                       # one of BUY|SELL|SHORT|COVER|HOLD
    symbol="TSLA",
    confidence=0.71,
    risk_multiplier=0.0,                 # 0 for HOLD, else 0..1
    rationale="bear_score 0.82 dominates bull 0.18; observation only",
    evidence={
        "bull_score": 0.18, "bear_score": 0.82,
        "regime_bucket": "high_vol", "horizon": "1h",
    },
    decision_id="dec_abc123",            # your internal id, optional
    regime="risk_off",                   # optional
)
await post_intent(intent)
```

---

## Step 3 — Set the flag

In your brain's `.env`:

```bash
DECISION_MACHINE_ENABLED=true
```

The flag is read at every `post_intent` call. Flipping it back to `false` (or deleting the line) stops new intent emissions immediately — **no restart needed**. Existing posts stay in MC's audit log.

---

## What MC does with the intent

The envelope you send:

```json
{
  "stack": "camaro",
  "action": "BUY",
  "symbol": "TSLA",
  "confidence": 0.71,
  "risk_multiplier": 0.75,
  "rationale": "...",
  "evidence": {...},
  "may_execute": false,
  "requires_gate_pass": true
}
```

The record MC stores (with server-stamped fields):

```json
{
  "intent_id": "uuid-v4",                  // MC-stamped
  "stack": "camaro",
  "action": "BUY",
  "symbol": "TSLA",
  "confidence": 0.71,
  "risk_multiplier": 0.75,
  "rationale": "...",
  "evidence": {...},
  "may_execute": false,                    // schema-pinned to False
  "requires_gate_pass": true,              // schema-pinned to True
  "seat_at_post_time": "executor",         // MC-stamped from live seat policy
  "ingest_ts": "2026-05-13T...",           // MC-stamped
  "ingest_method": "runtime_token",
  "gate_state": "pending",                 // pending|passed|blocked|dry_run_*
  "executed": false,
  "executed_at": null,
  "execution_receipt_id": null
}
```

Until the broker adapter lands (paper-trading Day 3), every intent sits at `gate_state="pending"`. The operator can run `POST /api/execution/dry_run?intent_id=...` to evaluate the gate chain without touching a broker. Day 2 lands real Gate objects (RoadGuard, $10 cap, executor-seat check). Day 3+ wires Alpaca paper.

---

## How to verify after rollout

From inside the canary brain's container:

```bash
python - <<'PY'
import asyncio, os
os.environ["DECISION_MACHINE_ENABLED"] = "true"
from services.decision_machine import Intent, post_intent, read_intents

async def main():
    out = await post_intent(Intent(
        stack=os.environ["RUNTIME_NAME"],
        action="HOLD",
        symbol="TSLA",
        confidence=0.5,
        risk_multiplier=0.0,
        rationale="decision_machine smoke test",
        evidence={"smoke": True},
    ))
    print("post:", out)
    recent = await read_intents(stack=os.environ["RUNTIME_NAME"], limit=3)
    print("recent:", [(x["intent_id"][:8], x["action"], x["symbol"]) for x in recent["items"]])

asyncio.run(main())
PY
```

Expected: `post:` shows `ok: True` + `seat_at_post_time` + `gate_state: pending`. `recent:` shows the new intent plus any prior ones.

From the operator console:

```bash
# Operator-authed: list all intents from this brain
curl "$MC/api/intents?stack=camaro&limit=10" \
  -H "X-Runtime-Token: $CAMARO_TOKEN"

# Operator-authed: run the dry-run gate chain on the intent
curl -X POST "$MC/api/execution/dry_run?intent_id=<the_uuid>" \
  -H "Authorization: Bearer $ADMIN_JWT"
```

The dry-run today returns 4 stub gate results all passing. Day 2 replaces the stubs with real RoadGuard / executor-seat / $10-cap checks.

---

## Rollback (no code change)

If anything looks wrong during the 24h observation window:

```bash
# In the brain's .env, change to:
DECISION_MACHINE_ENABLED=false
# Or delete the line entirely.
```

Next call to `post_intent` reads the flag and short-circuits. Already-posted intents stay in MC's audit log (which is what you want for forensic review). No mutation, no deletion, no risk.

---

## Hard guarantees

- **Brain cannot grant itself execution authority.** `may_execute=true` returns 422 from MC's schema validator.
- **Brain cannot bypass the gate chain.** `requires_gate_pass=false` returns 422.
- **Brain cannot post as another brain.** `X-Runtime-Token` must match `body.stack` or you get 401.
- **Brain cannot self-stamp a role.** `seat_at_post_time` comes from MC's seat registry, not the envelope.
- **No order is placed by this patch.** The broker adapter is a separate module that lands Day 3 and only reads intents whose `gate_state == "passed"`.

---

## What this does NOT do (yet)

- Does NOT call a broker
- Does NOT enforce $10/order cap (lands Day 2)
- Does NOT check the executor seat (lands Day 1 of paper sprint)
- Does NOT generate position lifecycle (lands Day 4)
- Does NOT replace your existing opinions/heartbeats flow — runs alongside

---

*Issued: 2026-02-13 · Mission Control · Decision Machine Patch v1.0*
*Status: opt-in via DECISION_MACHINE_ENABLED env flag*
