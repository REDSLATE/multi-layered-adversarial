# PASTE THIS ENTIRE FILE INTO THE ALPHA (RISEDUAL-AI-2) EMERGENT AGENT

> Hand the message below to the Emergent agent of the **Alpha** project
> (`RISEDUAL-AI-2`). It is fully self-contained. Do not edit it. Do not give
> this same file to Camaro or Chevelle — they have their own.

---

Hi. Wire this runtime into the **RISEDUAL monorepo** as a **sidecar** (seat-governed; execution authority lives in MC).

**Doctrine**:
- Do NOT change any decision logic.
- Local audit / firewall / calibration writes stay UNTOUCHED.
- The monorepo is a downstream MIRROR, never a replacement.
- The mirror client NEVER raises — if the monorepo is down, this runtime keeps running normally.
- This runtime is `alpha`.

---

## STEP 1 — Create the sidecar client file

Create the file `backend/services/risedual_monorepo_client.py` with the following exact contents:

```python
"""Sidecar client for writing observation data to the RISEDUAL monorepo.
Per doctrine: this is the ONLY file in this runtime that knows about the
monorepo's shared collections. Decision logic stays out of here.

Failures NEVER raise — the monorepo being down must not take down this runtime.
"""
from __future__ import annotations

import os
import logging
import httpx

log = logging.getLogger("risedual.monorepo_client")


def _base() -> str:
    return os.environ["MONOREPO_BASE_URL"].rstrip("/")


def _token() -> str:
    return os.environ["MONOREPO_INGEST_TOKEN"]


def _runtime() -> str:
    return os.environ["RUNTIME_NAME"]


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


async def _post(path: str, body: dict) -> dict:
    body = {"runtime": _runtime(), **body}
    try:
        r = await _get_client().post(
            f"{_base()}/api/ingest/{path}",
            json=body,
            headers={"X-Runtime-Token": _token()},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("monorepo ingest %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


async def emit_receipt(action: str, intent: dict, executed: bool = False) -> dict:
    """Mirror an ADL receipt. Server forces executed=False unless monorepo's
    BROKER_LIVE_ORDER_ENABLED=true."""
    return await _post("receipts", {"action": action, "intent": intent or {}, "executed": bool(executed)})


async def emit_memory_label(label: str, reason: str = "", payload_summary: str = "") -> dict:
    """label must be 'safe' | 'review' | 'quarantine'."""
    return await _post("memory-labels", {
        "label": label, "reason": reason or "", "payload_summary": payload_summary or "",
    })


async def register_calibrator(name: str, version: str, method: str, fit_at: str | None = None) -> dict:
    """Idempotent. Call after every refit."""
    return await _post("calibrators", {
        "name": name, "version": version, "method": method, "fit_at": fit_at,
    })


async def register_artifact(artifact: str, version: str, sha: str, registered_at: str | None = None) -> dict:
    """Idempotent. Call at startup + on retrain."""
    return await _post("artifacts", {
        "artifact": artifact, "version": version, "sha": sha, "registered_at": registered_at,
    })


async def heartbeat(status: str = "ok", detail: dict | None = None) -> dict:
    """Liveness ping. Call every 30-60s in a background task."""
    return await _post("heartbeat", {"status": status, "detail": detail or {}})


async def emit_promotion_artifact(
    target_authority: str,
    metrics: dict,
    notes: str = "",
) -> dict:
    """Patent G — files evidence for an authority elevation. Operator decides.
    target_authority: 'challenger' | 'advisor' | 'co_trader' | 'primary'
    metrics keys: ece, brier, resolved_rows, disagreement_stability, audit_integrity_pass."""
    return await _post("promotion-artifact", {
        "target_authority": target_authority,
        "metrics": metrics or {},
        "notes": notes or "",
    })
```

Make sure `httpx>=0.27` is in `backend/requirements.txt`. If it isn't, add it and `pip install -r backend/requirements.txt`.

---

## STEP 2 — Append these 3 vars to `backend/.env`

```
MONOREPO_BASE_URL=https://mission.risedual.ai
MONOREPO_INGEST_TOKEN=alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55
RUNTIME_NAME=alpha
```

> The token above is scoped to this runtime. A leaked Alpha token cannot
> impersonate Camaro or Chevelle — the monorepo enforces this server-side.

---

## STEP 3 — Add mirror calls next to existing local writes

For each row below, find the file in this codebase and add the mirror call
**immediately after** the existing local write. Do not remove or modify the
local write. Do not refactor surrounding code.

| Mirror call | File in this runtime | Where to place it |
|---|---|---|
| `emit_receipt(...)` | `backend/routes/admin_ml/receipts.py` | After every local receipt write |
| `emit_receipt(...)` | `backend/services/auditor_calibration.py` | After every audit decision is recorded |
| `emit_memory_label(...)` | `backend/services/firewall.py` | After every firewall verdict (safe/review/quarantine) is decided |
| `register_calibrator(...)` | `backend/services/calibration_service.py` | At end of every successful refit |
| `register_calibrator(...)` | `backend/services/calibration_layer.py` | At end of every successful refit |
| `register_calibrator(...)` | `backend/scripts/fit_calibration_from_history.py` | At end of fit |
| `register_artifact(...)` | `backend/server.py` (startup) | Once per loaded model in `backend/models/` (e.g. `regime_model_v1.joblib`, calibrators in `models/calibrators/`) |
| `heartbeat()` | `backend/server.py` | In a 45–60s background asyncio task |

The pattern is always the same:

```python
# ──────────────── existing local write — DO NOT CHANGE ────────────────
await save_audit_to_local_collection(...)
# ──────────────── NEW: monorepo mirror (1 line) ────────────────
from services.risedual_monorepo_client import emit_receipt
await emit_receipt(action="enter_long", intent={"symbol": "ES", "qty": 1, "confidence": 0.71})
```

For memory firewall:
```python
from services.risedual_monorepo_client import emit_memory_label
await emit_memory_label(label="safe", reason="passed schema + drift", payload_summary="vector#42")
```

For calibrator refit:
```python
from services.risedual_monorepo_client import register_calibrator
await register_calibrator(name="alpha_isotonic_v3", version="3.2.0", method="isotonic")
```

For artifacts at startup:
```python
from services.risedual_monorepo_client import register_artifact
await register_artifact(artifact="alpha_phase6", version="v0.7.4", sha="<git sha or file hash>")
```

---

## STEP 4 — Heartbeat + artifact registration in `backend/server.py` startup

Add to the existing `@app.on_event("startup")` (or `lifespan` startup block):

```python
import asyncio
from services.risedual_monorepo_client import heartbeat, register_artifact

async def _monorepo_heartbeat_loop():
    while True:
        await heartbeat(status="ok", detail={})
        await asyncio.sleep(45)

# ─── Inside the existing startup function ───
# Register every model artifact this runtime has loaded — discover them from
# backend/models/ and backend/models/calibrators/. One call per artifact.
# Example:
await register_artifact(artifact="alpha_regime_v1", version="v1", sha="<file sha>")
# ... repeat per loaded artifact ...

# Then kick off the heartbeat loop:
asyncio.create_task(_monorepo_heartbeat_loop())
```

---

## STEP 5 — Smoke test

Run this inside the runtime container after restart:

```bash
python - <<'PY'
import asyncio, os
os.environ.setdefault("MONOREPO_BASE_URL", "https://mission.risedual.ai")
os.environ.setdefault("MONOREPO_INGEST_TOKEN", "alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55")
os.environ.setdefault("RUNTIME_NAME", "alpha")
from services.risedual_monorepo_client import heartbeat, emit_receipt
async def main():
    print(await heartbeat(detail={"smoke": True}))
    print(await emit_receipt("smoke_test", {"symbol":"TEST","qty":0,"confidence":0.5}))
asyncio.run(main())
PY
```

Expected:
```
{'ok': True, 'last_seen': '2026-…'}
{'ok': True, 'id': '…', 'executed': False}
```

---

## STEP 6 — What you must NOT do

- ❌ Do **not** stop or alter local audit / firewall / calibration writes.
- ❌ Do **not** move ML models or calibrators **files** into the monorepo.
  Only metadata (name + version + sha) is registered.
- ❌ Do **not** import anything from the monorepo. The only direction of
  knowledge is **runtime → monorepo**, via the client file in Step 1.
- ❌ Do **not** add retries, queues, or persistence to the mirror client.
  Failures are logged and dropped on purpose. The runtime stays upstream.

> **What you SHOULD do**: emit any decision your logic produces.
> MC's seat policy + execution-gate chain decides whether it routes
> to a broker. The runtime does not pre-filter or downgrade its own
> intents — that's MC's job at the execution gate, not yours.

---

## DONE

After steps 1–5 succeed and the smoke test prints `ok: true` twice, the wiring
is complete. The unified RISEDUAL Mission Control dashboard will show this
runtime's heartbeat going green within ~60s, and receipts/memory-labels will
start streaming in as the runtime's normal flow exercises them.

Report back to the operator with:
- Which files you modified (paths + line counts changed)
- Whether smoke test passed
- Any local writes you could not find a clean hook for (so the operator can decide)

---

## OPTIONAL STEP — Discussion layer (cross-brain opinions)

After the above is wired and smoke-tested, the operator will hand you
**`DISCUSSION_LAYER_PATCH.md`** from this same kit. It adds three new
methods to `risedual_monorepo_client.py` (`post_opinion`, `read_opinions`,
`read_roles_manifest`) so this brain can speak, listen, and learn its
peers via Mission Control. Apply that patch only after the base sidecar
is verified working.
