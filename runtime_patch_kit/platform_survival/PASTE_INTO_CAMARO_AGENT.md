# PASTE-INTO Camaro Agent — Platform Survival Layer

Camaro is currently in a `challenger` seat and its intents are
correctly downgraded to `shadow_proposal` by MC. After this paste,
every shadow intent Camaro emits carries a verifiable PROD-vs-preview
stamp, so the Promotion-Artifact evidence pipeline can trust the
provenance of each sample. **This unblocks promotion math** — the
operator can finally tell whether 1100 shadow proposals came from the
PROD Camaro or from a preview deploy that drifted in.

## 1. Drop the module

```
cp -r runtime_patch_kit/platform_survival/services/platform_survival \
      <CAMARO_REPO>/backend/services/platform_survival
cp runtime_patch_kit/platform_survival/tests/test_platform_survival.py \
   runtime_patch_kit/platform_survival/tests/test_no_duplicate_execution_gates.py \
      <CAMARO_REPO>/backend/tests/
```

## 2. Boot stamp

```python
from services.platform_survival import RuntimeStamp

@app.on_event("startup")
async def _stamp_runtime():
    stamp = RuntimeStamp.current(sidecar_room="camaro_room")
    app.state.runtime_stamp = stamp
```

## 3. Replace Camaro's intent emit

Camaro is the CRYPTO_EXECUTOR candidate. Use the canonical adapter so
MC's classifier knows the role:

```python
from services.platform_survival import sidecar_build_intent
from services.platform_survival.role_adapters import camaro_emit_crypto_intent

# Canonical executor-intent shape.
exec_intent = camaro_emit_crypto_intent(
    symbol=symbol,
    direction=direction,        # BUY / SELL / HOLD
    confidence=conf,
    notional_usd=notional_usd,
)
# Wrap in the survival envelope (RuntimeStamp).
intent = sidecar_build_intent(
    brain_id="camaro", lane="crypto", symbol=symbol,
    direction=direction, confidence=conf, room_id="camaro_room",
)
intent.update(exec_intent)
await mc_post("/api/ingest/intent", json=intent)
```

Doctrine:
- BUY / SELL above 0.30 confidence → MC classifies as
  `executable_candidate`
- HOLD (or any non-directional) → MC classifies as
  `NON_DIRECTIONAL_OPINION` and persists to the ledger WITHOUT
  routing it through the gate chain (no more HOLD spam on Intents)

This also kills the noisy "10 HOLDs in 27 minutes" pattern — those
intents are now filtered at MC's classifier instead of clogging the
council gates.

## 4. Env vars on Camaro

| Variable | Value |
| --- | --- |
| `RISEDUAL_APP_NAME` | `camaro` |
| `RISEDUAL_ENV` | `prod` |
| `RISEDUAL_PLATFORM` | your hosting |
| `RISEDUAL_MC_URL` | `https://mission.risedual.ai` |
| `RISEDUAL_DB_NAME` | Camaro's PROD DB |
| `RISEDUAL_BROKER_MODE` | `paper` (until seat promotion) |
| `RISEDUAL_SIDECAR_VERSION` | semver |
| `GIT_SHA` | build-time hash |

⛔ Same rule as Alpha — **never set `RISEDUAL_MC_RECEIPT_SECRET` on
Camaro**.

## 5. Why this matters for Camaro specifically

The Promotion-Artifact report on Mission Control
(`/api/admin/promotion-artifact/camaro`) reads from every Camaro intent
in `shared_intents`. Today those rows have no PROD/preview marker — the
operator must trust the source IP and timestamp. After this paste, every
row carries `runtime.env_name`, `runtime.git_sha`, `runtime.platform`,
and `runtime.policy_hash`. The promotion report can filter to
`env_name == "prod"` only, killing the preview-drift noise that's
currently inflating the sample size.

## 6. Verification

```
cd <CAMARO_REPO>/backend
pytest tests/test_platform_survival.py -q
```

Expect 4 pass.

## 7. Next step after this lands

Once Camaro is emitting stamped intents, MC's promotion report
auto-becomes more trustworthy. If the hit-rate + agreement-rate cross
the 30% floors with N ≥ 20 PROD-only samples, the operator can
countersign Camaro's promotion to a `co_trader` seat via
`/admin/promotion/proposals`.
