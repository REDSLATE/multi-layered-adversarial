# Paste Into ALPHA Agent — Code Evolution v0

Alpha is the **Trader**. It's the only stack with execution authority on
the trading ladder, which makes the Code Evolution gate especially
important here: any patch touching the live execution path classifies
**CRITICAL** and requires **dual-sign** (mirrors Mission Control's Build 3).

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

## 2. Wire `deps.py` (Alpha-flavoured)

Use the template in `INTEGRATION.md`, then append Alpha's specifics:

```python
# At the bottom of deps.py, after the standard wiring:
from services.code_evolution import ast_invariants as _ai

# Alpha-specific: strategy modules are execution-adjacent.
_ai.EXECUTION_PATHS = list(_ai.EXECUTION_PATHS) + [
    "alpha/strategy",
    "alpha/order_router",
    "alpha/phase6",
]

# Alpha-specific guardrails on top of the defaults.
_ai.FORBIDDEN_ASSIGNMENTS["ALPHA_LIVE_ENABLED"] = lambda v: v is True
_ai.FORBIDDEN_ASSIGNMENTS["PHASE6_ENFORCE_ENABLED"] = lambda v: v is True
```

## 3. Mount the router

```python
# backend/server.py (or wherever Alpha's FastAPI app lives)
from services.code_evolution.api import router as code_evolution_router
app.include_router(code_evolution_router, prefix="/api")
```

## 4. What changes for Alpha specifically

- **Any** patch touching `alpha/strategy/`, `alpha/order_router/`,
  `alpha/phase6/`, or any path containing `execution`/`broker`/`order` →
  CRITICAL → 2 distinct operators must countersign.
- Patches to risk/sizing → HIGH → 24h cool-down.
- Patches to Code Evolution itself → BLOCKED → 423. Out-of-band only.

## 5. Verify

```bash
python3 runtime_patch_kit/code_evolution/smoke_test.py
```

9/9 OK is the contract. Then run the live `curl` smoke from `INTEGRATION.md`
against Alpha's live URL.
