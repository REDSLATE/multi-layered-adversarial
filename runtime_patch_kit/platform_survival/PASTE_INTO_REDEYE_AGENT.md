# PASTE-INTO REDEYE Agent — Platform Survival Layer

REDEYE holds the `opponent` seat. Its opinions and crypto-side intents
flow through MC's discussion layer + the REDEYE-Camaro intent bridge.
After this paste, every opinion REDEYE posts and every crypto intent
the bridge generates carries a verifiable PROD-vs-preview stamp.

## 1. Drop the module

```
cp -r runtime_patch_kit/platform_survival/services/platform_survival \
      <REDEYE_REPO>/backend/services/platform_survival
cp runtime_patch_kit/platform_survival/tests/test_platform_survival.py \
   runtime_patch_kit/platform_survival/tests/test_no_duplicate_execution_gates.py \
      <REDEYE_REPO>/backend/tests/
```

## 2. Boot stamp

```python
from services.platform_survival import RuntimeStamp

@app.on_event("startup")
async def _stamp_runtime():
    stamp = RuntimeStamp.current(sidecar_room="redeye_room")
    app.state.runtime_stamp = stamp
```

## 3. Stamp every opinion + crypto intent

For opinions posted via `/api/opinions`:

```python
from dataclasses import asdict

payload = {
    "runtime": "redeye",
    "stance": stance,
    "confidence": conf,
    "evidence": evidence,
    "runtime_stamp": asdict(app.state.runtime_stamp),  # NEW
}
await mc_post("/api/opinions", json=payload)
```

For crypto intents via the REDEYE→Camaro intent bridge:

```python
from services.platform_survival import sidecar_build_intent

intent = sidecar_build_intent(
    brain_id="redeye",
    lane="crypto",
    symbol=symbol,
    direction=direction,        # BUY / SELL
    confidence=conf,
    room_id="redeye_room",
)
await mc_post("/api/ingest/intent/crypto", json=intent)
```

## 4. Env vars on REDEYE

| Variable | Value |
| --- | --- |
| `RISEDUAL_APP_NAME` | `redeye` |
| `RISEDUAL_ENV` | `prod` |
| `RISEDUAL_PLATFORM` | your hosting |
| `RISEDUAL_MC_URL` | `https://mission.risedual.ai` |
| `RISEDUAL_DB_NAME` | REDEYE's PROD DB |
| `RISEDUAL_BROKER_MODE` | `paper` |
| `RISEDUAL_SIDECAR_VERSION` | semver |
| `GIT_SHA` | build-time hash |

⛔ Never set `RISEDUAL_MC_RECEIPT_SECRET` on REDEYE.

## 5. Verification

```
cd <REDEYE_REPO>/backend
pytest tests/test_platform_survival.py -q
```

Expect 4 pass.

## 6. Why this matters

REDEYE-PROD currently shows uptime/heartbeat data but the operator
flagged that it's been pinging without sovereign contributions. The
stamp makes it explicit which env every contribution came from, so
MC's `runtime_stamp.env_name="prod"` filter on
`shared_brain_opinions` and `shared_intents` becomes trivial.
