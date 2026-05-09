# Code Evolution — Stack Integration Guide

The package is identical for every stack. The **only** stack-specific file
is `services/code_evolution/deps.py`. This document shows the exact
template for each of the four RISEDUAL stacks.

---

## 1. Drop the package

```
cp -r services/code_evolution  <YOUR_STACK_REPO>/backend/services/code_evolution
```

The package consumes nothing your stack doesn't already have:
`fastapi`, `pydantic`, `motor` (or `pymongo`).

---

## 2. Edit `deps.py` — the only file you touch

Open `<YOUR_STACK_REPO>/backend/services/code_evolution/deps.py` and replace
the two `NotImplementedError` stubs with the template below. The template
assumes JWT-cookie auth + Motor + Mongo, which every RISEDUAL stack uses.

```python
"""Code Evolution dependency wiring — STACK-SPECIFIC."""
from __future__ import annotations

import os
from typing import Any

import jwt
from fastapi import HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient

from .receipts import MotorDispatcher, ReceiptDispatcher

# ─── storage ────────────────────────────────────────────────────────────
_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
_db = _client[os.environ["DB_NAME"]]
_dispatcher: ReceiptDispatcher = MotorDispatcher(_db)


async def get_dispatcher() -> ReceiptDispatcher:
    return _dispatcher


# ─── auth ──────────────────────────────────────────────────────────────
# Adapt to whatever your existing auth layer does. The function MUST raise
# HTTPException(401) on missing/invalid token, and return a dict with at
# least an "email" field for countersign tracking.
async def get_current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(
            token,
            os.environ["JWT_SECRET"],
            algorithms=["HS256"],
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}") from e
    return {
        "id": payload.get("sub"),
        "email": payload.get("email"),
    }
```

That's it. Eight lines for storage, fifteen for auth.

---

## 3. Mount the router

In your stack's main FastAPI app file (usually `backend/server.py` or
`backend/main.py`):

```python
from services.code_evolution.api import router as code_evolution_router

app.include_router(code_evolution_router, prefix="/api")
```

The router itself uses prefix `/admin/code-evolution`, so mounted under
`/api` it becomes `/api/admin/code-evolution/*`.

---

## 4. Per-stack tweaks (optional)

Each stack typically wants slightly different forbidden patterns. Override
in your stack's `deps.py` **after** importing the defaults — do **not**
edit `ast_invariants.py` directly (that's a PROTECTED file).

```python
# Append your stack's specifics — runs at import time, applies to every audit.
from services.code_evolution import ast_invariants

ast_invariants.EXECUTION_PATHS = list(ast_invariants.EXECUTION_PATHS) + [
    "alpha/strategy",         # Alpha-only: strategy modules are execution-adjacent
]

ast_invariants.FORBIDDEN_ASSIGNMENTS["MAX_DAILY_TRADES"] = (
    lambda v: isinstance(v, int) and v > 50
)
```

> **Note.** Even though `ast_invariants` is in `code_evolution/`, mutating
> module-level constants from `deps.py` is allowed because it doesn't change
> the *file* — just the in-memory config. Patches that propose to edit the
> file itself are still BLOCKED by PROTECTED_PATHS.

---

## 5. Mongo collection

The `MotorDispatcher` writes one collection: `code_evolution_proposals`.
No index migration is required for v0; the dispatcher upserts on
`proposal_id`. If you want a TTL on stale proposals, add it directly in
your stack's index setup — Code Evolution doesn't manage indexes for you.

```python
# Optional: in your existing ensure_indexes() routine
await db["code_evolution_proposals"].create_index("proposal_id", unique=True)
await db["code_evolution_proposals"].create_index([("created_at", -1)])
await db["code_evolution_proposals"].create_index([("status", 1), ("created_at", -1)])
```

---

## 6. Smoke-test inside your stack

```bash
# 1. Login, capture token
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"...","password":"..."}' | jq -r .access_token)

# 2. Submit a LOW patch
curl -s -X POST "$BASE/api/admin/code-evolution/audit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "noop",
    "target_files": ["backend/utils/strings.py"],
    "rationale": "smoke",
    "diff_text": "...",
    "post_patch_files": {"backend/utils/strings.py": "x = 1\n"}
  }' | jq

# 3. Try to patch the gate itself → expect 423
curl -s -i -X POST "$BASE/api/admin/code-evolution/audit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "tamper",
    "target_files": ["backend/services/code_evolution/api.py"],
    "rationale": "should fail",
    "diff_text": "...",
    "post_patch_files": {"backend/services/code_evolution/api.py": "x = 1\n"}
  }' | head -5
```

---

## What you should NOT change

| Don't edit | Why |
|---|---|
| `promotion_policy.may_auto_promote` | Doctrine line. Source of "AI may not promote" guarantee. |
| `ast_invariants.PROTECTED_PATHS` | Source of "AI may not modify the gate" guarantee. |
| `api.py` | Logic is shared across all stacks; per-stack overrides go in `deps.py`. |
| `schemas.py` | Wire format — must match across stacks for any future cross-stack receipt aggregation. |

If you find yourself wanting to edit any of the above, that's the moment
to file the change as a **doctrine PR** that goes through every stack's
operators in lock-step, not a stack-local patch.
