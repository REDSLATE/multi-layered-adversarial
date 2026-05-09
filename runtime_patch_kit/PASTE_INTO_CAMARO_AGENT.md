# PASTE THIS ENTIRE FILE INTO THE CAMARO (RD4_0421) EMERGENT AGENT

> Hand the message below to the Emergent agent of the **Camaro** project
> (`RD4_0421`). It is fully self-contained. Do not edit it. Do not give this
> same file to Alpha or Chevelle — they have their own.

---

Hi. Wire this runtime into the **RISEDUAL monorepo** as a **sidecar** (observation-only mirror).

**Doctrine**:
- Do NOT change any decision logic.
- Local audit / firewall / calibration writes stay UNTOUCHED.
- The monorepo is a downstream MIRROR, never a replacement.
- The mirror client NEVER raises — if the monorepo is down, this runtime keeps running normally.
- This runtime is `camaro`.

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
MONOREPO_INGEST_TOKEN=camaro-ingest-7b2e1f8a-9c1d-4e2b-8a3f-2d6c4e8f1b09
RUNTIME_NAME=camaro
```

> This token is scoped to this runtime. A leaked Camaro token cannot impersonate
> Alpha or Chevelle — the monorepo enforces this server-side.

---

## STEP 3 — Add mirror calls next to existing local writes

For each row below, find the file in this codebase and add the mirror call
**immediately after** the existing local write. Do not remove or modify the
local write. Do not refactor surrounding code.

| Mirror call | File in this runtime | Where to place it |
|---|---|---|
| `emit_receipt(...)` | `backend/services/decision_audit.py` | After every audit/decision write |
| `emit_receipt(...)` | `backend/services/executors/camaro_executor.py` | When a shadow row is produced (executor proposal) |
| `emit_memory_label(...)` | `backend/services/chevelle_memory_labeler.py` | After every label decision — pass `runtime: "camaro"` (use the env var, automatic via the client) |
| `register_calibrator(...)` | `backend/services/calibration_layer.py` | At end of every successful refit |
| `register_calibrator(...)` | `backend/services/calibration.py` | At end of every successful fit |
| `register_artifact(...)` | `backend/server.py` (startup) | Once per `models/<symbol>.joblib` (AAPL, AMZN, BTC-USD, GOOGL, JPM, META, MSFT, NVDA, SPY, TSLA, XOM) |
| `heartbeat()` | `backend/server.py` | In a 45–60s background asyncio task |

The pattern is always the same:

```python
# ──────────────── existing local write — DO NOT CHANGE ────────────────
await record_decision_audit(...)
# ──────────────── NEW: monorepo mirror (1 line) ────────────────
from services.risedual_monorepo_client import emit_receipt
await emit_receipt(action="executor_proposed", intent={"symbol": "GC", "side": "long", "size": 2})
```

For shadow rows (Camaro's executor):
```python
from services.risedual_monorepo_client import emit_receipt
await emit_receipt(
    action="shadow_proposal",
    intent={"symbol": "ES", "side": shadow.side, "size": shadow.size, "confidence": shadow.confidence},
    executed=False,  # always false in observation mode
)
```

For memory firewall:
```python
from services.risedual_monorepo_client import emit_memory_label
await emit_memory_label(label="safe", reason="passed schema + drift", payload_summary="batch#…")
```

For calibrator refit:
```python
from services.risedual_monorepo_client import register_calibrator
await register_calibrator(name="camaro_isotonic_v2", version="2.0.7", method="isotonic")
```

For artifacts at startup:
```python
from services.risedual_monorepo_client import register_artifact
await register_artifact(artifact="camaro_lgbm_AAPL", version="v0.4.2", sha="<file sha>")
```

> NOTE on `chevelle_memory_labeler.py` inside Camaro: yes, this file exists in
> Camaro's codebase even though it's named "chevelle". Per doctrine, when this
> file runs **inside Camaro's process**, it should mirror with
> `runtime: "camaro"`. The `runtime` value is set automatically by the client
> from `RUNTIME_NAME` in this runtime's `.env` (which is `camaro`). Just call
> `emit_memory_label(...)`; do not pass an explicit runtime override.

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
# Register every per-symbol joblib in backend/models/ once.
# Example:
for symbol in ["AAPL", "AMZN", "BTC-USD", "GOOGL", "JPM", "META", "MSFT", "NVDA", "SPY", "TSLA", "XOM"]:
    await register_artifact(artifact=f"camaro_lgbm_{symbol}", version="v0.4.2", sha="<file sha>")

# Also register the adversarial sidecar artifact if loaded:
await register_artifact(artifact="camaro_adversary", version="v0.1.0", sha="<file sha>")

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
os.environ.setdefault("MONOREPO_INGEST_TOKEN", "camaro-ingest-7b2e1f8a-9c1d-4e2b-8a3f-2d6c4e8f1b09")
os.environ.setdefault("RUNTIME_NAME", "camaro")
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
- ❌ Do **not** trust the monorepo for execution gating. This runtime owns its
  own enforce flags (`CAMARO_EXECUTOR_ENFORCE_ENABLED`, broker controls, etc.).
- ❌ Do **not** flip any enforce flag during this work. Stay observation-only.
- ❌ Do **not** import anything from the monorepo. The only direction of
  knowledge is **runtime → monorepo**, via the client file in Step 1.
- ❌ Do **not** add retries, queues, or persistence to the mirror client.
  Failures are logged and dropped on purpose. The runtime stays upstream.

---

## DONE

After steps 1–5 succeed and the smoke test prints `ok: true` twice, the wiring
is complete. The unified RISEDUAL Mission Control dashboard will show this
runtime's heartbeat going green within ~60s, and shadow-row receipts /
memory-labels will start streaming in as the runtime's normal flow exercises them.

Report back to the operator with:
- Which files you modified (paths + line counts changed)
- Whether smoke test passed
- Any local writes you could not find a clean hook for (so the operator can decide)
