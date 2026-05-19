# Alpha — wire `mc_checkin.py` into the sidecar

## Step 1 — Drop the file in

```
Alpha repo:
  services/mc_checkin/__init__.py   ← paste the contents of mc_checkin.py
```

## Step 2 — Add env vars to Alpha's `.env` / Railway / Render config

```bash
RISEDUAL_MC_URL=https://mission.risedual.ai
ALPHA_MC_INGEST_TOKEN=alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55
RISEDUAL_ENV=prod
RISEDUAL_PLATFORM=railway              # or render / fly / etc.
RISEDUAL_DB_NAME=<alpha's prod mongo DB name>
RISEDUAL_BROKER_MODE=paper             # paper | live | dry_run
RISEDUAL_SIDECAR_VERSION=1.0.0
RISEDUAL_APP_NAME=alpha
GIT_SHA=<set by CI to the commit alpha boots from>

# Optional — defaults to 300s (5 min)
RISEDUAL_MC_CHECKIN_INTERVAL_SECONDS=300
```

> `ALPHA_MC_INGEST_TOKEN` above is from MC's `backend/.env`
> `ALPHA_INGEST_TOKEN`. Same token, mirrored on the Alpha side.

## Step 3 — Call it from Alpha's boot

If Alpha runs a FastAPI app:

```python
# alpha/main.py (or wherever the FastAPI app is created)
from services.mc_checkin import (
    checkin_now,
    start_periodic_checkin,
    stop_periodic_checkin,
)

@app.on_event("startup")
async def _mc_checkin_startup():
    try:
        await checkin_now()             # synchronous boot ping
    except Exception:
        # Don't block boot on MC being flaky — but log loudly.
        import logging
        logging.getLogger("alpha").exception("mc_checkin boot ping failed")
    start_periodic_checkin(app.state)   # 5-min periodic ping in bg

@app.on_event("shutdown")
async def _mc_checkin_shutdown():
    await stop_periodic_checkin()
```

If Alpha runs as a long-lived asyncio daemon (no FastAPI):

```python
from services.mc_checkin import checkin_now, start_periodic_checkin

async def main():
    await checkin_now()
    start_periodic_checkin()
    # ... rest of Alpha's main loop ...
```

## Step 4 — Verify on MC

After Alpha redeploys, open MC's Diagnostics dashboard and scroll to
**"Sidecar identity check-ins"**. The Alpha row should flip from
`NEVER` → `PROD`, with all stamp fields populated. The summary line
at the top should now show `1 prod · 0 preview · 0 drift · 3 never`.

Or hit MC's API directly:

```bash
curl -s -H "Authorization: Bearer <admin JWT>" \
  https://mission.risedual.ai/api/admin/runtime/sidecar-checkin/alpha \
  | jq .
```

Expected (clean prod):

```json
{
  "runtime": "alpha",
  "verdict": "prod",
  "freshness": "fresh",
  "checkin_count": 1,
  "policy_hash_match": true,
  "stamp": {
    "env_name": "prod",
    "mc_url": "https://mission.risedual.ai",
    "local_execution_authority": false,
    ...
  },
  "errors": []
}
```

## Failure modes — what each verdict means

| Verdict        | What it means                                        | Operator action                              |
|----------------|------------------------------------------------------|----------------------------------------------|
| `prod`         | Clean stamp, policy hash matches MC                  | None — Alpha is correctly in PROD            |
| `preview`      | `RISEDUAL_ENV != "prod"` or `RISEDUAL_MC_URL` wrong  | Fix Alpha's env vars; redeploy               |
| `policy_drift` | Stamp valid but Alpha's `_POLICY` dict ≠ MC's        | Pull latest `mc_checkin.py` from MC; redeploy|
| `invalid`      | Stamp shape rejected by MC validator                 | See `errors[]`; fix env vars                 |
| `never`        | Alpha hasn't POSTed yet                              | Confirm boot hook ran; check Alpha logs      |

## Doctrine note

This client is OBSERVABILITY ONLY. It does NOT carry execution
authority and `local_execution_authority` is hard-pinned to `False`
in the dataclass. The broker still verifies MC's HMAC-signed
receipt before any Alpaca / Kraken call — that's the lock on bad
orders. This check-in is the operator's "is Alpha actually where I
think it is?" tripwire, nothing more.
