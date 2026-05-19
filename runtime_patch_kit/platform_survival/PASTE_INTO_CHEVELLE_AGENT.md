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
