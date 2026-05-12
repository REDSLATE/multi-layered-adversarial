# Onboarding Packet — CHEVELLE brain

> **Hand this to the agent / operator working on the Chevelle brain host.**
> Self-contained: contains everything needed to connect Chevelle to Mission Control.

## Identity

- **Brain name**: `chevelle`
- **Personality**: Risk auditor — quality / calibration focused. Default-skeptical;
  willing to abstain when conviction is low instead of forcing a vote.
- **Default seat (today)**: Governor. Holds the veto bit in the seat policy.
- **Mode for first run**: `DTD` (deterministic training data — safe to learn weights).

## Mission Control connection details

```bash
MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
CHEVELLE_INGEST_TOKEN="chevelle-ingest-d4a8e6c2-1b5f-4a3d-9e7c-3f8b1a5c6d72"
```

> ⚠️ The `MC_BASE_URL` above is the preview URL; when MC goes to prod, the operator
> updates one env var on the host — no code change needed.

## Suggested initial weights (Chevelle — calibration / risk bias)

```python
# Risk auditor — balanced features, low learning rate (slow, deliberate)
CHEVELLE_INITIAL_WEIGHTS = {
    "trend": 0.35,
    "macd":  0.35,
    "rsi":   0.35,    # balanced — Chevelle is a generalist that vetoes on signals
}
```

Critically, **lower the learning rate**: Chevelle should change its mind slowly.
A governor that flips its stance every other bar is a useless governor.

```python
from runtime_patch_kit.sovereign.local_state import LocalState
state = LocalState(brain="chevelle", mode="DTD")
if not state.weights:
    state.set_weights({"trend": 0.35, "macd": 0.35, "rsi": 0.35})
    state.set_learning_rate(0.02)    # slow, deliberate updates — governor doctrine
    state.save()
```

## Quick start (host machine, 5 minutes)

```bash
# 1. Copy the sovereign kit to this host
cd runtime_patch_kit/sovereign

# 2. Confirm Python 3.11+
python3 -V

# 3. Set the env vars
export MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
export CHEVELLE_INGEST_TOKEN="chevelle-ingest-d4a8e6c2-1b5f-4a3d-9e7c-3f8b1a5c6d72"

# 4. Verify doctrine locks before connecting (no MC needed)
python3 smoke_test.py     # expect 8/8 PASS

# 5. Start the sidecar
python3 sidecar.py --brain chevelle --mode DTD --symbols BTC/USD ETH/USD --interval 60
```

Within ~60 seconds MC will show Chevelle in `GET /api/admin/sovereign/state` and the
operator's `/runtime/chevelle` page will render the Sovereign State tile.

## Suggested symbols for Chevelle

Chevelle is a generalist that audits across everything the other brains touch. Set
its symbol list to be the **union** of what Alpha and Camaro watch:

- **Crypto**: `BTC/USD ETH/USD SOL/USD`
- **Equities** (when feeders ship): `SPY QQQ NVDA TSLA`

## Doctrine reminders (non-negotiable)

1. `LIVE_TRADING_ENABLED` must stay False in `wild_adaptive_core_v2.py`.
2. Never write directly to MC's DB — only `mc_client.py` HTTP endpoints.
3. PRD mode disallows training. To learn, switch to DTD and replay historical bars.

## Why Chevelle's role matters

Chevelle holds the **veto bit**. When Alpha and the executor are aligned on a LONG
but Chevelle posts SHORT or abstains, the system records that as
`flagged_by_auditor=true` in MC and risedual.ai's UI renders the
RISK_AUDITOR_AGENT block as `VETO · DISSENT_AGAINST_STRATEGIST`. That's exactly
how you avoid the "all four AIs agreed and were all wrong" failure mode.

For that veto to mean anything, Chevelle has to be **honestly skeptical**. Don't
tune it to agree with the others. If the operator finds Chevelle vetoing too
often, the fix is to refine its calibration (DTD replay against historical
outcomes), not to weaken its skepticism.

## Verifying from MC's side

```bash
curl -s "$MC_BASE_URL/api/admin/sovereign/state/chevelle" \
  -H "Authorization: Bearer $OPERATOR_JWT"
```

## When something goes wrong

See `runtime_patch_kit/sovereign/DEPLOY.md` for the troubleshooting matrix.

## Notes about Chevelle's existing site

Chevelle previously had a standalone frontend. Direction C retires that site —
public users go to risedual.ai (which is itself becoming a face for ALL four
brains via MC). The Chevelle brain on this host should focus on its governance
role; the standalone site doesn't need to be kept running.

If for any reason that site is still serving traffic, it should:
- Not call MC's `/api/admin/*` endpoints (operator-only).
- Not write to MC's DB.
- Either be shut down or migrated to consume MC's `/api/public/*` namespace via the
  same `mcPublicClient.ts` pattern as risedual.ai.

## Questions for the operator

1. Which host runs Chevelle?
2. systemd or Docker?
3. Confirm: is Chevelle's old standalone site going to be turned off?
4. Run the synthetic stub for 24h, or wire the broker feed immediately?
