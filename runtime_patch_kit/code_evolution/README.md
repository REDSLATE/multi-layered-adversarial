# Code Evolution v0 — Patch Kit

> **Doctrine.** Code Evolution is allowed to judge code. Code Evolution is
> not allowed to become the source of authority for code.
>
> ```
> AI may audit code.
> AI may recommend tests.
> AI may write receipts.
> AI may NOT run shell commands.
> AI may NOT promote code.
> AI may NOT modify its own gate.
> ```

This kit is identical across all four RISEDUAL stacks (Alpha, Camaro,
Chevelle, REDEYE). Each stack hosts its own copy. Each stack writes its
own receipts to its own Mongo. There is no cross-stack auto-promotion.

---

## Architecture (one stack)

```
operator pastes patch
        │
        ▼
   POST /api/admin/code-evolution/audit
        │
        ▼
   ast_invariants.py  ─── PROTECTED?  → 423 BLOCKED (out-of-band only)
        │                CRITICAL?    → AWAITING_SIGNATURE (dual-sign)
        │                HIGH/MED/LOW?→ AWAITING_SIGNATURE (single-sign)
        ▼
   code_auditor.py    ─── classification + required_tests + cool_down
        │
        ▼
   receipts.py        ─── code_evolution_proposals (Mongo)
        │
        ▼
   POST /api/admin/code-evolution/{id}/countersign
        │   (operator email; same operator cannot sign twice)
        ▼
   APPROVED → operator applies the patch out-of-band.
              Code Evolution NEVER writes to disk.
              promotion_policy.may_auto_promote() returns False, period.
```

---

## Files in this kit

| Path | Role |
|---|---|
| `services/code_evolution/__init__.py` | Re-exports, doctrine docstring |
| `services/code_evolution/schemas.py` | Dataclasses + Pydantic IO models |
| `services/code_evolution/ast_invariants.py` | AST walker, PROTECTED / EXECUTION / RISK paths, FORBIDDEN_ASSIGNMENTS, FORBIDDEN_CALLS |
| `services/code_evolution/code_auditor.py` | Classifier (LOW/MEDIUM/HIGH/CRITICAL/PROTECTED) + required-tests checklist |
| `services/code_evolution/promotion_policy.py` | `may_auto_promote() → False` (the keys-holder line) |
| `services/code_evolution/receipts.py` | `ReceiptDispatcher` Protocol + InMemory + Motor adapters |
| `services/code_evolution/deps.py` | **The only file each stack edits.** Wires auth + storage. |
| `services/code_evolution/api.py` | FastAPI router (audit / list / countersign / reject) |
| `smoke_test.py` | 9-test doctrine verifier — runs without Mongo or HTTP server |
| `INTEGRATION.md` | Step-by-step host wiring |
| `PASTE_INTO_<STACK>_AGENT.md` | Per-stack copy-paste shell |

---

## STEP 1 — Drop the package into your stack

Copy the entire `services/code_evolution/` folder into your stack's backend at:

```
backend/services/code_evolution/
```

(No new dependencies beyond FastAPI, Pydantic, and Motor — all of which
every RISEDUAL stack already has.)

---

## STEP 2 — Wire auth + storage

Edit **only** `backend/services/code_evolution/deps.py`. Replace the two
`NotImplementedError` stubs with your stack's `get_current_user` (from your
existing JWT auth) and a `MotorDispatcher` bound to your stack's Mongo db.
The full template is in the file's docstring; details in
[INTEGRATION.md](./INTEGRATION.md).

---

## STEP 3 — Mount the router in your FastAPI app

```python
from services.code_evolution.api import router as code_evolution_router

app.include_router(code_evolution_router, prefix="/api")
```

---

## STEP 4 — Verify before pasting

From this folder:

```bash
python3 smoke_test.py
```

Expected: 9/9 OK with the doctrine line at the bottom.

---

## STEP 5 — Test the live endpoint inside your stack

```bash
curl -X POST "$STACK_URL/api/admin/code-evolution/audit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "rename helper",
    "target_files": ["backend/utils/strings.py"],
    "rationale": "tighten naming",
    "diff_text": "...",
    "post_patch_files": {
      "backend/utils/strings.py": "def upper(s):\n    return s.upper()\n"
    }
  }'
```

Expected: HTTP 200, `classification: "LOW"`, `required_signatures: 1`.

Try a patch to `backend/services/code_evolution/api.py` and confirm
HTTP 423 — the gate refuses to audit changes to itself in-band.

---

## What this kit deliberately does NOT do

- **Does not run shell commands** — there is no `subprocess` import in any
  file. The `test_runner.py` from the v1 sketch is not shipped. Each stack
  runs its own tests through its own existing pipeline.
- **Does not write code to disk** — the `APPROVED` status means "operator
  may now apply"; the operator applies the patch via their existing path
  (git, supervisor reload, etc.). No file mutation.
- **Does not promote across stacks** — each stack has its own audit trail.
  Code Evolution is doctrinally local.
- **Does not let AI bypass the gate** — `may_auto_promote()` is a hard
  `False`. Patches that touch the gate are PROTECTED → 423.

---

## Operator quick reference

| Classification | What it means | What you do |
|---|---|---|
| **PROTECTED** | Patch touches the gate itself. | Out-of-band edit only — direct git commit by an operator with shell access. |
| **CRITICAL**  | Patch touches live execution / broker / order paths. | Two distinct operators must countersign (mirrors Build 3). 24h cool-down. |
| **HIGH**      | Patch touches risk or direction logic. | Single operator countersign. 24h cool-down. |
| **MEDIUM**    | Invariant scan surfaced something weird (forbidden assignment / drift / syntax). | Single sign. Read the audit notes carefully. |
| **LOW**       | Pure refactor, no risky paths. | Single sign. Receipt only. |

---

## Curriculum hook (forward-compat)

If the operator later wants the AI to *propose* patches (a Code Strategist),
that strategist:

1. Lives **outside** this package.
2. POSTs to `/api/admin/code-evolution/audit` like any operator.
3. Gets the same audit + invariant + classification.
4. **Cannot** countersign (no email = no sign).
5. **Cannot** apply (no write path).

That asymmetry is the doctrine. Strategist proposes; operator decides.
