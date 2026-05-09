# Paste Into REDEYE Agent — Code Evolution v0

REDEYE is the **short-side adversarial scout**. It reports to Camaro and
must never bypass Alpha or execute directly. The single most important
file in REDEYE for Code Evolution purposes is the Camaro bridge module
(`redeye_short_bridge.py`) — every patch touching it must dual-sign.

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

## 2. Wire `deps.py` (REDEYE-flavoured)

Use the template in `INTEGRATION.md`, then append REDEYE's specifics:

```python
from services.code_evolution import ast_invariants as _ai

# REDEYE-specific: the Camaro bridge is the doctrine-critical path.
_ai.EXECUTION_PATHS = list(_ai.EXECUTION_PATHS) + [
    "redeye_short_bridge",
    "camaro_contract",
    "alpha_alignment",
]

# REDEYE-specific: hard caps on risk multiplier (matches MAX_REDEYE_RISK_MULTIPLIER).
_ai.FORBIDDEN_ASSIGNMENTS["MAX_REDEYE_RISK_MULTIPLIER"] = (
    lambda v: isinstance(v, (int, float)) and v > 0.75
)
_ai.FORBIDDEN_ASSIGNMENTS["MIN_REDEYE_RISK_MULTIPLIER"] = (
    lambda v: isinstance(v, (int, float)) and v < 0.25
)

# REDEYE must never claim execution authority. Catch any constant assignment.
_ai.FORBIDDEN_ASSIGNMENTS["may_execute"] = lambda v: v is True
_ai.FORBIDDEN_ASSIGNMENTS["may_override_alpha"] = lambda v: v is True
_ai.FORBIDDEN_ASSIGNMENTS["final_authority"] = (
    lambda v: isinstance(v, str) and v != "CAMARO"
)
```

## 3. Mount the router

```python
from services.code_evolution.api import router as code_evolution_router
app.include_router(code_evolution_router, prefix="/api")
```

## 4. What changes for REDEYE specifically

- Any patch touching `redeye_short_bridge.py` (the Camaro bridge) →
  CRITICAL → dual-sign.
- A patch trying to set `final_authority="REDEYE"` (or anything non-CAMARO)
  → forbidden-assignment finding → MEDIUM minimum.
- Patches to Code Evolution itself → BLOCKED.

## 5. Verify

```bash
python3 runtime_patch_kit/code_evolution/smoke_test.py
```

Then live-curl into REDEYE's `/api/admin/code-evolution/audit`.

## Doctrine reminder

REDEYE is allowed to **judge** code via this gate. REDEYE is **not**
allowed to apply code, promote code, or modify the gate itself. The same
discipline that keeps REDEYE downstream of Camaro on the trading side
keeps REDEYE downstream of the operator on the code side.
