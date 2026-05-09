# RISEDUAL Integration Contract — Sidecar Mode

**Doctrine**: one shared nervous system, three separate decision brains.

This document is the contract between the **monorepo (mission control)** and the
three runtime brains (Alpha, Camaro, Chevelle). Each runtime keeps its own
process, its own ML artifacts, its own calibrators, its own runtime flags, and its
own decision authority. It only **writes** observation/audit data into the shared
infrastructure described here.

---

## What the runtime keeps to itself

- `server.py`, all routes, all services, all ML models
- All internal Mongo collections (signals, portfolios, council outputs, etc.)
- All `.env` flags that govern execution authority (e.g. `PHASE6_ENFORCE_ENABLED`,
  `CAMARO_EXECUTOR_ENFORCE_ENABLED`, `CHEVELLE_AUTHORITY_ENABLED`, `BROKER_LIVE_ORDER_ENABLED`)
- All promotion gates and broker controls

## What the runtime writes through the shared APIs

| Concept | Endpoint | When to call |
|---|---|---|
| Decision intent (every action proposal/decision) | `POST /api/ingest/receipts` | Every time the runtime proposes or executes an action |
| Memory firewall label | `POST /api/ingest/memory-labels` | Every time the firewall labels a payload safe / review / quarantine |
| Calibrator metadata | `POST /api/ingest/calibrators` | After every successful refit (idempotent on `runtime+name`) |
| Model artifact metadata | `POST /api/ingest/artifacts` | At startup + after any retrain (idempotent on `runtime+artifact`) |
| Liveness | `POST /api/ingest/heartbeat` | Every 30–60 seconds |

> The runtime never reads from the shared APIs. The unified dashboard reads.

---

## Authentication

Every ingest call must include the header:

```
X-Runtime-Token: <token>
```

The token must match the env var named after the runtime:

| Runtime | Env var (set in monorepo `.env`) |
|---|---|
| `alpha` | `ALPHA_INGEST_TOKEN` |
| `camaro` | `CAMARO_INGEST_TOKEN` |
| `chevelle` | `CHEVELLE_INGEST_TOKEN` |

Each runtime gets its own token. The server checks that the runtime named in the
request body matches the runtime that owns the token. **A leaked Alpha token
cannot impersonate Camaro or Chevelle.**

Each runtime stores its token in **its own** `.env` as a single variable —
recommended name: `MONOREPO_INGEST_TOKEN`. The base URL is also a runtime env:
`MONOREPO_BASE_URL` (e.g. `https://mission.risedual.ai`).

---

## Endpoints (request / response)

### POST /api/ingest/receipts
```json
// request
{
  "runtime": "alpha" | "camaro" | "chevelle",
  "action": "enter_long" | "exit" | "phase6_proposal" | "...",
  "intent": { "symbol": "ES", "qty": 1, "confidence": 0.71 },
  "executed": false
}
// response
{ "ok": true, "id": "<uuid>", "executed": false }
```

> **Observation invariant**: even if the runtime claims `executed=true`, the
> server forces it to `false` unless the monorepo's
> `BROKER_LIVE_ORDER_ENABLED=true`. Receipts are an observation ledger first.

### POST /api/ingest/memory-labels
```json
// request
{
  "runtime": "chevelle",
  "label": "safe" | "review" | "quarantine",
  "reason": "passed schema + drift checks",
  "payload_summary": "feature_vector batch #324"
}
// response
{ "ok": true, "id": "<uuid>" }
```

### POST /api/ingest/calibrators
```json
// request — idempotent upsert on (runtime, name)
{
  "runtime": "camaro",
  "name": "camaro_isotonic_v2",
  "version": "2.0.7",
  "method": "isotonic",
  "fit_at": "2026-05-09T08:24:14Z"   // optional; defaults to now
}
// response
{ "ok": true }
```

### POST /api/ingest/artifacts
```json
// request — idempotent upsert on (runtime, artifact)
{
  "runtime": "alpha",
  "artifact": "alpha_phase6",
  "version": "v0.7.4",
  "sha": "a1b2c3d4",
  "registered_at": "2026-05-09T08:24:14Z"   // optional
}
// response
{ "ok": true }
```

### POST /api/ingest/heartbeat
```json
// request
{
  "runtime": "alpha",
  "status": "ok" | "degraded" | "down",
  "detail": { "queue_depth": 0, "last_decision_ms": 87 }
}
// response
{ "ok": true, "last_seen": "<iso>" }
```

---

## Drop-in Python client (≈ 30 LOC)

Save as `risedual_monorepo_client.py` inside any runtime's backend.

```python
"""Sidecar client for writing observation data to the RISEDUAL monorepo.
Per doctrine: this is the ONLY file in this runtime that knows about the
monorepo's shared collections. Decision logic stays out of here."""
import os
import logging
import httpx

log = logging.getLogger("monorepo_client")

BASE = os.environ["MONOREPO_BASE_URL"].rstrip("/")
TOKEN = os.environ["MONOREPO_INGEST_TOKEN"]
RUNTIME = os.environ["RUNTIME_NAME"]  # "alpha" | "camaro" | "chevelle"

_client = httpx.AsyncClient(timeout=5.0)


async def _post(path: str, body: dict) -> dict:
    body = {"runtime": RUNTIME, **body}
    try:
        r = await _client.post(
            f"{BASE}/api/ingest/{path}",
            json=body,
            headers={"X-Runtime-Token": TOKEN},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("monorepo ingest %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


async def emit_receipt(action: str, intent: dict, executed: bool = False):
    return await _post("receipts", {"action": action, "intent": intent, "executed": executed})


async def emit_memory_label(label: str, reason: str = "", payload_summary: str = ""):
    return await _post("memory-labels", {"label": label, "reason": reason, "payload_summary": payload_summary})


async def register_calibrator(name: str, version: str, method: str, fit_at: str | None = None):
    return await _post("calibrators", {"name": name, "version": version, "method": method, "fit_at": fit_at})


async def register_artifact(artifact: str, version: str, sha: str, registered_at: str | None = None):
    return await _post("artifacts", {"artifact": artifact, "version": version, "sha": sha, "registered_at": registered_at})


async def heartbeat(status: str = "ok", detail: dict | None = None):
    return await _post("heartbeat", {"status": status, "detail": detail or {}})
```

### Wiring it in (for each runtime)

1. **`.env`** of the runtime gets:
   ```
   MONOREPO_BASE_URL=https://<your-monorepo-host>
   MONOREPO_INGEST_TOKEN=<token-for-this-runtime>
   RUNTIME_NAME=alpha   # or camaro / chevelle
   ```
2. **`audit_trail.py`** (or equivalent): after every existing local audit write,
   add `await emit_receipt(action, intent, executed)`. Keep your local audit too —
   the monorepo is a **mirror**, not a replacement.
3. **`memory_labeler.py`** (or equivalent): after every local label decision,
   add `await emit_memory_label(label, reason, payload_summary)`.
4. **`calibration_writer.py` / `calibration_scheduler.py`**: after every successful
   refit, add `await register_calibrator(name, version, method, fit_at)`.
5. **Startup**: register every loaded artifact once, e.g.
   ```python
   await register_artifact("alpha_xgb", "v0.7.4", sha)
   ```
6. **Background task**: every 30–60s call `await heartbeat()`.

That's the whole port. Each runtime keeps its current behavior; it just gains a
mirror in the monorepo.

---

## What you do **not** do

- Do not stop writing to your own internal collections. The monorepo is observation
  and dashboard layer; your runtime stays self-sufficient.
- Do not move ML models or calibrators into the monorepo. Only metadata is
  registered there.
- Do not flip any enforce flag because the dashboard says you can. Promotion gates
  belong to each runtime, individually, out-of-band.
- Do not authenticate with admin credentials. The admin is for human operators of
  the dashboard; runtimes use their per-runtime ingest token.

---

## Failure semantics

The client logs `monorepo ingest <path> failed: <err>` and returns
`{"ok": false}`. **It never raises.** A monorepo outage must not take down a
runtime. The runtime is the source of truth for its own behavior; the monorepo
is downstream.

---

## Verification (run from any runtime host)

```bash
curl -s -X POST "$MONOREPO_BASE_URL/api/ingest/heartbeat" \
  -H "Content-Type: application/json" \
  -H "X-Runtime-Token: $MONOREPO_INGEST_TOKEN" \
  -d "{\"runtime\":\"$RUNTIME_NAME\",\"status\":\"ok\",\"detail\":{}}"
```

If you get `{"ok": true, "last_seen": "..."}`, you're wired in. Open the
mission-control dashboard's Diagnostics tab and you'll see your runtime's
heartbeat row update.
