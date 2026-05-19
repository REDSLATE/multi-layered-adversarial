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

REDEYE is the OPPONENT. Use the canonical adapter for oppositions:

```python
from services.platform_survival import RuntimeStamp, sidecar_build_intent
from services.platform_survival.role_adapters import redeye_emit_opposition
from dataclasses import asdict

# Opposition (advisory — DOES NOT kill trades alone per doctrine):
opp = redeye_emit_opposition(
    symbol=symbol,
    lane="equity",                # or "crypto"
    direction=primary_direction,
    confidence=conf,
    opposes=True,
)
opp["runtime_stamp"] = asdict(app.state.runtime_stamp)
await mc_post("/api/opinions", json=opp)
```

For crypto intents via the REDEYE→Camaro intent bridge, REDEYE uses
the executor-shape adapter (it's emitting an executable candidate,
not an opposition):

```python
from services.platform_survival.role_adapters import camaro_emit_crypto_intent

# REDEYE-side bridge: emit a directional crypto candidate.
intent = camaro_emit_crypto_intent(
    symbol=symbol, direction=direction,
    confidence=conf, notional_usd=notional,
)
intent["brain"] = "redeye"      # override
intent["role"] = "crypto_executor"
envelope = sidecar_build_intent(
    brain_id="redeye", lane="crypto", symbol=symbol,
    direction=direction, confidence=conf, room_id="redeye_room",
)
envelope.update(intent)
await mc_post("/api/ingest/intent/crypto", json=envelope)
```

Doctrine: REDEYE oppositions count as adversary evidence weight in
the council — they do NOT kill trades by themselves. Only the
governor (Chevelle) hard veto or structural safety stops will block
execution.

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
