# Paste Into CAMARO Agent — Code Evolution v0

Camaro is the **Challenger / final live decision authority**. It receives
REDEYE's short-side advisories and Alpha's long thesis, and is the only
brain with the audit-trail wrapper around the REDEYE bridge. Two paths
deserve extra-tight Code Evolution protection:

1. The REDEYE bridge wrapper (`redeye_short_bridge.py`,
   `redeye_features.py`, `redeye_long_short_focus.py`).
2. The Camaro Commander's final-decision module.

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

## 2. Wire `deps.py` (Camaro-flavoured)

Use the template in `INTEGRATION.md`, then append Camaro's specifics:

```python
from services.code_evolution import ast_invariants as _ai

# Camaro-specific: REDEYE bridge wrapper + Camaro Commander are execution-adjacent.
_ai.EXECUTION_PATHS = list(_ai.EXECUTION_PATHS) + [
    "camaro_commander",
    "redeye_short_bridge",
    "redeye_features",
    "redeye_long_short_focus",
    "research/redeye",
]

# Camaro-specific: never tolerate `may_execute=true` injected anywhere.
_ai.FORBIDDEN_ASSIGNMENTS["may_execute"] = lambda v: v is True
_ai.FORBIDDEN_ASSIGNMENTS["may_override_alpha"] = lambda v: v is True
```

## 3. Mount the router

```python
# backend/server.py (or wherever Camaro's FastAPI app lives)
from services.code_evolution.api import router as code_evolution_router
app.include_router(code_evolution_router, prefix="/api")
```

## 4. What changes for Camaro specifically

- Any patch touching the **REDEYE bridge wrapper** or
  **Camaro Commander** → CRITICAL → dual-sign.
- Any patch trying to set `may_execute=True` or `may_override_alpha=True`
  anywhere in the file → MEDIUM (surfaced as forbidden assignment) — these
  are the two doctrine flags REDEYE relies on Camaro to honour.
- Patches to Code Evolution itself → BLOCKED.

## 5. Verify

```bash
python3 runtime_patch_kit/code_evolution/smoke_test.py
```

Then live-curl into Camaro's `/api/admin/code-evolution/audit`.
