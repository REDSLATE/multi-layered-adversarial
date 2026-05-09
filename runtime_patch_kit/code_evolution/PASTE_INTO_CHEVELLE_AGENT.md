# Paste Into CHEVELLE Agent — Code Evolution v0

Chevelle is the **Governor**. It holds the keys: memory firewall, readiness
gate, calibration gate, audit verification, promotion control. Chevelle
itself is *off-ladder* on the trading side — it does not trade. But for
Code Evolution that makes Chevelle's gate doubly important: Chevelle is
where the doctrine lives.

## 1. Drop the package

```
backend/services/code_evolution/
  __init__.py
  schemas.py
  ast_invariants.py
  code_auditor.py
  promotion_policy.py
  receipts.py
  deps.py
  api.py
```

(Source: `runtime_patch_kit/code_evolution/services/code_evolution/`)

## 2. Wire `deps.py` (Chevelle-flavoured)

Use the template in `INTEGRATION.md`, then append Chevelle's specifics:

```python
from services.code_evolution import ast_invariants as _ai

# Chevelle-specific: governance + memory firewall are execution-adjacent
# in the sense that a bad patch here breaks every other stack's safety.
_ai.EXECUTION_PATHS = list(_ai.EXECUTION_PATHS) + [
    "memory_firewall",
    "readiness_gate",
    "calibration_gate",
    "audit_verify",
    "promotion_control",
    "authority_call",
]

# Chevelle-specific guardrails. The Governor must never be flipped on
# the trading ladder by a patch.
_ai.FORBIDDEN_ASSIGNMENTS["CHEVELLE_AUTHORITY_ENABLED"] = lambda v: v is True
_ai.FORBIDDEN_ASSIGNMENTS["GOVERNOR_TRADES"] = lambda v: v is True
```

## 3. Mount the router

```python
from services.code_evolution.api import router as code_evolution_router
app.include_router(code_evolution_router, prefix="/api")
```

## 4. What changes for Chevelle specifically

- Patches touching the **memory firewall**, **readiness gate**,
  **calibration gate**, **audit verifier**, or **promotion control** →
  CRITICAL → dual-sign.
- Any patch trying to set `CHEVELLE_AUTHORITY_ENABLED=True` or
  `GOVERNOR_TRADES=True` → flagged as forbidden assignment (Chevelle is
  off-ladder by doctrine).
- Patches to Code Evolution itself → BLOCKED.

## 5. Verify

```bash
python3 runtime_patch_kit/code_evolution/smoke_test.py
```

Then live-curl into Chevelle's `/api/admin/code-evolution/audit`.
