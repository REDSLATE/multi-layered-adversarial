# PASTE-INTO Chevelle Agent — Platform Survival Layer

Chevelle holds the `governor` seat in equity. Its authority calls
already gate equity execution via MC. After this paste, every authority
call Chevelle emits carries a verifiable PROD-vs-preview stamp, so MC
can tell whether a `block` or `modulate` verdict came from the PROD
Chevelle or from a preview drift.

## 1. Drop the module

```
cp -r runtime_patch_kit/platform_survival/services/platform_survival \
      <CHEVELLE_REPO>/backend/services/platform_survival
cp runtime_patch_kit/platform_survival/tests/test_platform_survival.py \
   runtime_patch_kit/platform_survival/tests/test_no_duplicate_execution_gates.py \
      <CHEVELLE_REPO>/backend/tests/
```

## 2. Boot stamp

```python
from services.platform_survival import RuntimeStamp

@app.on_event("startup")
async def _stamp_runtime():
    stamp = RuntimeStamp.current(sidecar_room="chevelle_room")
    app.state.runtime_stamp = stamp
```

## 3. Tag every authority call

Chevelle is the GOVERNOR. Use the canonical adapter so MC's
governor_policy module reads the status + reason directly:

```python
from services.platform_survival import RuntimeStamp
from services.platform_survival.role_adapters import chevelle_emit_authority
from dataclasses import asdict

stamp = app.state.runtime_stamp

# Canonical authority-call shape — what MC's governor_policy reads.
authority = chevelle_emit_authority(
    symbol=symbol,
    lane="equity",                                # or "crypto"
    status="BLOCK" if veto else "ALLOW",          # ALLOW / WARN / BLOCK
    reason="GOVERNOR_HARD_VETO" if veto else "NO_GOVERNOR_DISSENT",
    confidence=conf,
)
authority["runtime_stamp"] = asdict(stamp)  # provenance
await mc_post("/api/ingest/receipts", json=authority)
```

CRITICAL: only use `status="BLOCK", reason="GOVERNOR_HARD_VETO"`
when Chevelle has genuine high-conviction evidence the trade must be
killed. MC's governor_policy treats `GOVERNOR_HARD_VETO` as FATAL
(stops execution). Lower-confidence dissent should use
`status="WARN", reason="<your-specific-warning>"` so MC applies
risk-down instead of killing the trade.

Doctrine pin: Chevelle's silence ≠ kill switch. If Chevelle simply
doesn't post an authority call for a symbol, MC's classifier emits
`NO_STANCE_LOW_EFFECTIVE_CONF` / `GOVERNOR_OFFLINE` and routes the
trade at half size, not zero.

## 4. Env vars on Chevelle

| Variable | Value |
| --- | --- |
| `RISEDUAL_APP_NAME` | `chevelle` |
| `RISEDUAL_ENV` | `prod` |
| `RISEDUAL_PLATFORM` | your hosting |
| `RISEDUAL_MC_URL` | `https://mission.risedual.ai` |
| `RISEDUAL_DB_NAME` | Chevelle's PROD DB |
| `RISEDUAL_BROKER_MODE` | `paper` (Chevelle never executes) |
| `RISEDUAL_SIDECAR_VERSION` | semver |
| `GIT_SHA` | build-time hash |

⛔ Chevelle MUST NEVER hold `RISEDUAL_MC_RECEIPT_SECRET`. The governor
seat does not sign execution; it gates.

## 5. Verification

```
cd <CHEVELLE_REPO>/backend
pytest tests/test_platform_survival.py -q
```

Expect 4 pass.

## 6. Why this matters

Chevelle's authority calls drive the equity council gate. If a preview
Chevelle accidentally pings PROD MC, every equity intent is gated by
fake authority calls. The stamp + policy_hash makes that condition
detectable in one query against `shared_adl_receipts`.

## 7. Sidecar check-in POST loop — REQUIRED, not in `services/platform_survival`

The kit gives you the `RuntimeStamp` object. It does NOT post it to
MC. Every brain team has to wire the POST loop themselves. Without it,
MC's `/admin/runtime/sidecar-checkin/chevelle` endpoint returns
`verdict: never · checkin_count: 0` regardless of how good your stamp
is, and the operator dashboard shows Chevelle red.

A drop-in reference implementation lives at
`runtime_patch_kit/alpha_mc_checkin/mc_checkin.py` in this repo —
copy that file into your services dir and rename the brain.

If writing it inline, the loop is:

```python
import asyncio, httpx, os
from dataclasses import asdict
from services.platform_survival import RuntimeStamp

CHEVELLE_TOKEN = os.environ["CHEVELLE_INGEST_TOKEN"]
MC_URL = os.environ["RISEDUAL_MC_URL"]  # https://mission.risedual.ai
CHECKIN_INTERVAL_SEC = 60                # same cadence as the other brains

async def _checkin_loop():
    """POST our RuntimeStamp to MC every minute. Best-effort —
    any error degrades to a logged warning, never crashes the boot."""
    while True:
        try:
            stamp = RuntimeStamp.current(sidecar_room="chevelle_room")
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    f"{MC_URL}/api/admin/runtime/sidecar-checkin/chevelle",
                    headers={"X-Runtime-Token": CHEVELLE_TOKEN},
                    # IMPORTANT: MC's CheckinRequest schema requires
                    # the stamp wrapped under a "stamp" key. Sending
                    # bare `asdict(stamp)` returns HTTP 422.
                    # See: backend/shared/runtime/sidecar_checkin.py
                    # class CheckinRequest(BaseModel): stamp: dict
                    json={"stamp": asdict(stamp)},
                )
                r.raise_for_status()
        except Exception as e:
            print(f"[checkin] failed: {e!r}", flush=True)
        await asyncio.sleep(CHECKIN_INTERVAL_SEC)

@app.on_event("startup")
async def _start_checkin():
    asyncio.create_task(_checkin_loop())
```

### Verification (the step that flips you green)

After redeploy, ask the operator to run:
```bash
curl -sH "Authorization: Bearer $OWNER_JWT" \
  "https://mission.risedual.ai/api/admin/runtime/sidecar-checkin/chevelle" \
  | jq '{verdict, errors, last_checkin_at, checkin_count}'
```

Expected within 60s of boot:
```json
{
  "verdict": "prod",
  "errors": [],
  "last_checkin_at": "<recent>",
  "checkin_count": 1
}
```

If `verdict` ≠ `prod` or `errors` is non-empty, match the error code
against the six-field validator (`env_name`, `db_name`, `mc_url`,
`broker_mode`, `git_sha`, `local_execution_authority`) and fix the
env var.

## 8. Market-data key force-refresh on the same loop (option a)

To give MC's `market_data_key_fetches` collection a continuous
heartbeat from Chevelle, piggyback on the existing sidecar tick.
Every 60th tick (~once/hour) force-refresh:

```python
# inside _checkin_loop's while True, alongside the POST above:
tick_count += 1
if tick_count % 60 == 0:
    await get_market_data_keys(force_refresh=True)
```

No new background task. MC's TTL on cached keys is 300s, so an hourly
force-refresh from your side is well within the window.

## 9. Doctrine summary

| Endpoint | Frequency | What it proves |
| --- | --- | --- |
| `POST /api/admin/runtime/sidecar-checkin/chevelle` | every 60s | "I exist as a PROD sidecar" — flips verdict `never` → `prod` |
| `POST /api/ingest/receipts` (authority calls) | per intent | "I'm the equity governor for this symbol" — gates the trade |
| `GET /api/admin/keys/market-data` | hourly (option a) | "I'm consuming the data proxy" — continuous activity signal |

Skip the first one and MC has no record of your identity, regardless
of how good the other two are.

