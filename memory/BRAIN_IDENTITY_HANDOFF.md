# Brain identity surface — handoff to Alpha / Camaro / Chevelle / RedEye

Mission Control needs each brain to ship an `identity` block on its
`GET /status` endpoint so the operator can see, in one chip on MC's
dashboard, whether the brain's check-in worker is eligible to start.

This replaces the "guess which env var is missing" hunts that produced
RedEye's 7-hour gap and Alpha's 2-hour silence.

## Drop-in module

Copy [`mc_identity_v1.py`](./mc_identity_v1.py) into your sidecar repo
(e.g. `sidecar/mc_identity_v1.py`). The module is brain-agnostic — same
file works for all 4 brains.

## Integration (3 lines at boot + 1 in /status handler)

```python
from mc_identity_v1 import build_identity_block, log_lifecycle, start_checkin_worker

# In boot path (runs once):
identity = build_identity_block(app_name="alpha", sidecar_version="2.4.1")
log_lifecycle(identity)
if identity["checkin_worker_eligible"]:
    start_checkin_worker(interval_s=300, on_tick=your_existing_ping_fn)

# In /status route:
@app.get("/status")
async def status():
    return {
        "identity": build_identity_block(app_name="alpha", sidecar_version="2.4.1"),
        # ... your existing fields (seats, heartbeat, etc.) ...
    }
```

## Required env vars (per brain deployment)

All four must be set for the chip to turn GREEN:

| Env var            | Purpose                                           |
| ------------------ | ------------------------------------------------- |
| `MC_URL`           | MC base URL — brain → MC periodic check-in target |
| `MC_INGEST_TOKEN`  | Token MC accepts on the check-in path             |
| `MC_BASE_URL`      | MC base URL — brain ← MC opinion-delivery target  |
| `HEARTBEAT_TOKEN`  | Token MC accepts on the opinion / heartbeat path  |

The two pairs are kept separate so each fails independently — when
one is broken, MC's chip names exactly that env var inline (e.g.
`WORKER: NOT ELIGIBLE · missing: HEARTBEAT_TOKEN`).

## Lifecycle log contract

Your boot path MUST log exactly one of:

```
INFO mc_checkin worker STARTED — periodic check-in every 300s
     (MC_URL set, MC_INGEST_TOKEN set, MC_BASE_URL set, HEARTBEAT_TOKEN set)
```

```
WARNING mc_checkin worker NOT STARTED — missing env vars: MC_INGEST_TOKEN, HEARTBEAT_TOKEN
```

The NOT STARTED branch must always name the missing vars — operator
greps this on the prod logs.

The provided `log_lifecycle()` function emits the contracted line
once per process boot. Don't reformat.

## Contract pinning

The field set and log format are **v1**. MC's testing agent pins them:
[`backend/tests/test_mc_identity_v1_contract.py`](../backend/tests/test_mc_identity_v1_contract.py).

If your brain has a strong reason to deviate (rename a field, add a
fifth env pair, etc.), bump to **v2** and dual-publish both shapes
during a deprecation window — don't silently change v1.

## How to verify your integration

Run the drop-in standalone before wiring it up:

```bash
$ python mc_identity_v1.py
WARNING brain.mc_identity: mc_checkin worker NOT STARTED — missing env vars: MC_URL, MC_INGEST_TOKEN, MC_BASE_URL, HEARTBEAT_TOKEN
identity block:
  app_name                     'selftest'
  ...
  checkin_worker_eligible      False
```

Set the four env vars and re-run — you should see the STARTED log.

## Operator-facing payoff

Once shipped, MC's `/admin/diagnostics` Brain Health tile shows a
**WORKER** row per brain:

| State            | Color  | Tile text                                            |
| ---------------- | ------ | ---------------------------------------------------- |
| All env set      | GREEN  | `WORKER: ELIGIBLE`                                   |
| One env missing  | RED    | `WORKER: NOT ELIGIBLE · missing: HEARTBEAT_TOKEN`    |
| Older sidecar    | GREY   | `WORKER: unknown`                                    |
| Proxy not wired  | GREY   | `WORKER: no upstream`                                |

Operator no longer curls, no longer greps logs to spot the failure.
