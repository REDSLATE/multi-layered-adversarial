# PASTE-INTO Alpha Agent — Platform Survival Layer

Alpha currently emits intents directly. After this paste, Alpha's
sidecar uses `sidecar_build_intent()` so every intent it sends MC
carries a verifiable runtime stamp (env, git_sha, platform, policy hash)
and proves it does NOT hold local execution authority.

## 1. Drop the module

```
cp -r runtime_patch_kit/platform_survival/services/platform_survival \
      <ALPHA_REPO>/backend/services/platform_survival
cp runtime_patch_kit/platform_survival/tests/test_platform_survival.py \
   runtime_patch_kit/platform_survival/tests/test_no_duplicate_execution_gates.py \
      <ALPHA_REPO>/backend/tests/
```

## 2. Boot stamp (one line in your FastAPI lifespan)

```python
from services.platform_survival import RuntimeStamp

@app.on_event("startup")
async def _stamp_runtime():
    stamp = RuntimeStamp.current(sidecar_room="alpha_room")
    app.state.runtime_stamp = stamp
    print("alpha runtime stamp:", stamp)
```

## 3. Replace your intent-emit helper

Wherever Alpha currently does the equivalent of:

```python
intent = {"brain_id": "alpha", "symbol": symbol, "direction": "BUY", ...}
await post_to_mc(intent)
```

Switch to the canonical role adapter. Alpha is a STRATEGIST — its
emissions are advisory opinions. MC classifies them and decides
whether they become executable based on Alpha's seat:

```python
from services.platform_survival import sidecar_build_intent
from services.platform_survival.role_adapters import alpha_emit_opinion

# Build the canonical opinion shape.
opinion = alpha_emit_opinion(
    symbol=symbol, lane="equity", direction="BUY", confidence=conf,
)
# Wrap it in the survival envelope so MC sees the runtime stamp.
intent = sidecar_build_intent(
    brain_id="alpha", lane="equity", symbol=symbol,
    direction="BUY", confidence=conf, room_id="alpha_room",
)
intent.update(opinion)
await post_to_mc(intent)
```

MC's classifier (`shared/intent_contract.py`) reads this shape and
returns:
- `executable_candidate=True` if Alpha holds the executor seat AND
  confidence ≥ 0.30
- `advisory_only=True` with reason `NON_DIRECTIONAL_OPINION` if
  Alpha emits HOLD/WAIT/NEUTRAL
- `advisory_only=True` with reason `CONFIDENCE_BELOW_EXEC_FLOOR` if
  conf < 0.30

The envelope's `local_execution_authority=False` is the doctrine
guard — MC rejects any intent claiming sidecar-side execution rights.

## 4. Env vars on the Alpha sidecar host

| Variable | Value |
| --- | --- |
| `RISEDUAL_APP_NAME` | `alpha` |
| `RISEDUAL_ENV` | `prod` / `preview` / `local` |
| `RISEDUAL_PLATFORM` | `railway` / `vps` / etc. |
| `RISEDUAL_MC_URL` | `https://mission.risedual.ai` |
| `RISEDUAL_DB_NAME` | the DB Alpha reads (NOT `preview`, NOT empty) |
| `RISEDUAL_BROKER_MODE` | `paper` / `live` / `dry_run` |
| `RISEDUAL_SIDECAR_VERSION` | semver |
| `GIT_SHA` | baked at build time |

⛔ **Do NOT set `RISEDUAL_MC_RECEIPT_SECRET` on Alpha.** That's the MC
HMAC key. If a sidecar can sign receipts, the gate is broken.

## 5. CI

Add to `.github/workflows/test.yml` (or your equivalent):

```yaml
- run: pytest backend/tests/test_platform_survival.py backend/tests/test_no_duplicate_execution_gates.py -q
```

The duplicate-gate test will fail the build if anyone ever re-adds
`may_execute = True`, `local_execution_authority = True`, or old
`if live_enabled / if paper_only / if observe_only / operator_lock_default`
gates anywhere outside the platform survival module.

## 6. Verification

```
cd <ALPHA_REPO>/backend
pytest tests/test_platform_survival.py -q
```

Expect 4 pass. Then ship.
