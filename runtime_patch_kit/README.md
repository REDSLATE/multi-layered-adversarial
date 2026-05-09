# RISEDUAL Runtime Patch Kit — Sidecar Wiring (per runtime)

This document is everything you need to wire **one runtime** (Alpha, Camaro, or
Chevelle) into the monorepo's shared infrastructure. Each runtime keeps its own
process, ML models, calibrators, decision logic, and broker controls. It only
gains a thin **mirror** to the monorepo so the unified dashboard can see it.

> **Doctrine reminder**: do not move logic into the monorepo. Add mirror calls
> *next to* your existing local audit/firewall/calibration writes. The local
> writes stay exactly as they are.

---

## STEP 1 — Drop in one file

Copy `risedual_monorepo_client.py` (in this same kit) into your runtime's
backend at:

```
backend/services/risedual_monorepo_client.py
```

That's the entire sidecar client. ~70 lines including comments.

Make sure `httpx` is in your runtime's `requirements.txt`. Most likely already is.
If not: `pip install httpx>=0.27`.

---

## STEP 2 — Add 3 vars to that runtime's `.env`

Pick the right token for the runtime you're wiring. The monorepo `.env` already
has all three. Pull only the one for the runtime you're patching.

### Alpha (`RISEDUAL-AI-2`)
```
MONOREPO_BASE_URL=https://b177ffdc-73ff-45fb-9ba4-f1e63e5e4274.preview.emergentagent.com
MONOREPO_INGEST_TOKEN=alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55
RUNTIME_NAME=alpha
```

### Camaro (`RD4_0421`)
```
MONOREPO_BASE_URL=https://b177ffdc-73ff-45fb-9ba4-f1e63e5e4274.preview.emergentagent.com
MONOREPO_INGEST_TOKEN=camaro-ingest-7b2e1f8a-9c1d-4e2b-8a3f-2d6c4e8f1b09
RUNTIME_NAME=camaro
```

### Chevelle (`conflict_090526_0410`)
```
MONOREPO_BASE_URL=https://b177ffdc-73ff-45fb-9ba4-f1e63e5e4274.preview.emergentagent.com
MONOREPO_INGEST_TOKEN=chevelle-ingest-d4a8e6c2-1b5f-4a3d-9e7c-3f8b1a5c6d72
RUNTIME_NAME=chevelle
```

> These tokens are scoped per runtime: a leaked Alpha token cannot impersonate
> Camaro or Chevelle. The monorepo enforces this at the ingest endpoint.
> Rotate by editing the monorepo `.env` and re-pasting the new value here.

---

## STEP 3 — Wire 4–6 single-line additions, alongside existing local writes

The pattern is always the same:

```python
# your existing local write (untouched)
await save_audit_to_local_collection(...)

# NEW: mirror line
from services.risedual_monorepo_client import emit_receipt
await emit_receipt(action="enter_long", intent={"symbol": "ES", "qty": 1, "confidence": 0.71})
```

Below is the per-runtime cheat sheet of where I'd place each call, based on what
your zip showed. **Use these as starting points** — your runtime's LLM/operator
should confirm exact line numbers.

### 🅰️ Alpha (`RISEDUAL-AI-2`)

| Mirror call | File where it belongs | Hook point |
|---|---|---|
| `emit_receipt(...)` | `backend/routes/admin_ml/receipts.py` | After every local receipt write |
| `emit_receipt(...)` | `backend/services/auditor_calibration.py` | After every audit decision |
| `emit_memory_label(...)` | `backend/services/firewall.py` | After every firewall verdict |
| `register_calibrator(...)` | `backend/services/calibration_service.py` *and* `backend/services/calibration_layer.py` | After every successful refit |
| `register_calibrator(...)` | `backend/scripts/fit_calibration_from_history.py` | At end of fit |
| `register_artifact(...)` | `backend/server.py` startup | For each loaded model in `backend/models/` |
| `heartbeat()` | `backend/server.py` | Asyncio background task, every 60s |

### 🅱️ Camaro (`RD4_0421`)

| Mirror call | File | Hook point |
|---|---|---|
| `emit_receipt(...)` | `backend/services/decision_audit.py` | After every audit write |
| `emit_receipt(...)` | `backend/services/executors/camaro_executor.py` | When a shadow row is produced |
| `emit_memory_label(...)` | `backend/services/chevelle_memory_labeler.py` (yes, the one inside Camaro) | After every label decision — `runtime: "camaro"` |
| `register_calibrator(...)` | `backend/services/calibration_layer.py` | After refit |
| `register_artifact(...)` | `backend/server.py` startup | For each `models/<symbol>.joblib` |
| `heartbeat()` | `backend/server.py` | Background task, every 60s |

### 🅲 Chevelle (`conflict_090526_0410`)

| Mirror call | File | Hook point |
|---|---|---|
| `emit_receipt(...)` | `backend/services/audit_trail.py` | After every audit write |
| `emit_memory_label(...)` | `backend/services/chevelle_memory_labeler.py` | After every label |
| `register_calibrator(...)` | `backend/services/calibration_writer.py` and `backend/services/calibration_scheduler.py` | After every write/scheduled run |
| `register_calibrator(...)` | `backend/services/calibration_layer.py` (the `refit` method) | After refit success |
| `register_artifact(...)` | `backend/server.py` startup | For `strategist.pt`, all `symbolic/*.joblib`, `calibration/latest.joblib` |
| `heartbeat()` | `backend/server.py` | Background task, every 60s |

---

## STEP 4 — Background heartbeat (drop into each runtime's `server.py`)

```python
# In server.py, near the top of @app.on_event("startup") or lifespan startup:
import asyncio
from services.risedual_monorepo_client import heartbeat, register_artifact

async def _heartbeat_loop():
    while True:
        await heartbeat(status="ok", detail={})
        await asyncio.sleep(45)

@app.on_event("startup")
async def _start_monorepo_mirror():
    # Register artifacts once on boot — see runtime-specific list above
    # await register_artifact("alpha_phase6", "v0.7.4", sha="…")
    asyncio.create_task(_heartbeat_loop())
```

---

## STEP 5 — Smoke test (run from inside the runtime container)

```bash
python - <<'PY'
import asyncio, os
os.environ.setdefault("MONOREPO_BASE_URL", "<paste from step 2>")
os.environ.setdefault("MONOREPO_INGEST_TOKEN", "<paste from step 2>")
os.environ.setdefault("RUNTIME_NAME", "alpha")  # or camaro / chevelle
from services.risedual_monorepo_client import heartbeat, emit_receipt
async def main():
    print(await heartbeat(detail={"smoke": True}))
    print(await emit_receipt("smoke_test", {"symbol":"TEST", "qty":0, "confidence":0.5}))
asyncio.run(main())
PY
```

Expected output:
```
{'ok': True, 'last_seen': '2026-…'}
{'ok': True, 'id': '…', 'executed': False}
```

Then open mission control → **Diagnostics**: your runtime's heartbeat row will
show "ok" with a fresh `last_seen`. Open **ADL Receipts** and filter by your
runtime: the smoke-test receipt appears.

---

## STEP 6 — What you should **not** do

- ❌ Do not stop your local audit/firewall/calibration writes. The monorepo is a
  mirror. Your runtime is still the source of truth for itself.
- ❌ Do not move ML models or calibrators *files* into the monorepo. Only
  metadata (name + version + sha) is registered.
- ❌ Do not use a single shared token across runtimes. Per-runtime tokens are
  the doctrine.
- ❌ Do not trust the monorepo to gate execution. Your runtime owns its own
  enforce flags and broker controls. The monorepo only **observes**.

---

## Order of operations I recommend

1. Wire **Alpha** first — it has the most existing receipt/firewall/calibration
   surface area, so you'll see the most signal.
2. Watch the dashboard for an hour. Confirm Alpha's heartbeat is steady, receipts
   flow with `executed=false`, memory labels show up.
3. Wire **Camaro** next.
4. Wire **Chevelle** last.
5. **Do not touch any enforce flag** until all three are visibly observing for at
   least a full day. Then build the promotion-gate workflow (operator
   sign-off + audit log) per runtime.

---

## Failure semantics (worth re-reading)

`risedual_monorepo_client.py` **never raises**. If the monorepo is down,
unreachable, returns 5xx, or rejects the token, the client logs a warning
(`monorepo ingest <path> failed: <err>`) and returns `{"ok": false}`. Your
runtime continues unaffected. This is intentional: the monorepo is downstream;
the runtime is upstream.
