# Onboarding Packet — CAMARO brain

> **Hand this to the agent / operator working on the Camaro brain host.**
> Self-contained: contains everything needed to connect Camaro to Mission Control.

## Identity

- **Brain name**: `camaro`
- **Personality**: Mean reverter — biased toward fading extremes, not chasing them.
- **Default seat (today)**: Advisor / Opponent. Counterweight to Alpha's trend bias.
- **Mode for first run**: `DTD` (deterministic training data — safe to learn weights).

## Mission Control connection details

```bash
MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
CAMARO_INGEST_TOKEN="camaro-ingest-7b2e1f8a-9c1d-4e2b-8a3f-2d6c4e8f1b09"
```

> ⚠️ The `MC_BASE_URL` above is the preview URL; when MC goes to prod, the operator
> updates one env var on the host — no code change needed.

## Suggested initial weights (Camaro — mean-reversion bias)

```python
# Mean reverter — fade trend, lean on RSI extremes
CAMARO_INITIAL_WEIGHTS = {
    "trend": -0.45,    # negative weight = lean against the trend
    "macd":   0.20,
    "rsi":    0.80,    # heavy weight on RSI = trust oversold / overbought signals
}
```

In code (one-time bootstrap, no-op after the state file exists):

```python
from runtime_patch_kit.sovereign.local_state import LocalState
state = LocalState(brain="camaro", mode="DTD")
if not state.weights:
    state.set_weights({"trend": -0.45, "macd": 0.20, "rsi": 0.80})
    state.set_learning_rate(0.05)
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
export CAMARO_INGEST_TOKEN="camaro-ingest-7b2e1f8a-9c1d-4e2b-8a3f-2d6c4e8f1b09"

# 4. Verify doctrine locks before connecting (no MC needed)
python3 smoke_test.py     # expect 8/8 PASS

# 5. Start the sidecar
python3 sidecar.py --brain camaro --mode DTD --symbols BTC/USD ETH/USD --interval 60
```

Within ~60 seconds MC will show Camaro in `GET /api/admin/sovereign/state` and the
operator's `/runtime/camaro` page will render the Sovereign State tile.

## Suggested symbols for Camaro

Mean-reversion does well on liquid range-bound markets. Defaults:

- **Crypto**: `BTC/USD ETH/USD` (the two highest-liquidity pairs)
- **Equities** (when feeders ship): `SPY QQQ` (index ETFs revert more cleanly than singles)

Pass via `--symbols BTC/USD ETH/USD`.

## Doctrine reminders (non-negotiable)

1. `LIVE_TRADING_ENABLED` must stay False in `wild_adaptive_core_v2.py`.
2. Never write directly to MC's DB — only `mc_client.py` HTTP endpoints.
3. PRD mode disallows training. To learn, switch to DTD and replay historical bars.

## Why Camaro matters in the architecture

Camaro's negative trend weight is the WHOLE POINT of having multiple brains. When
Alpha says "go long on this breakout!", Camaro says "but the RSI is at 78 and
this is exactly the kind of squeeze that fails." The system's intelligence
comes from these two perspectives disagreeing AND a seat policy that knows
which voice to give weight to in which situation.

If you copy Alpha's weights into Camaro, you've created a clone — which means
adversarial-blindness on every position. Keep the personalities distinct.

## Verifying from MC's side

```bash
curl -s "$MC_BASE_URL/api/admin/sovereign/state/camaro" \
  -H "Authorization: Bearer $OPERATOR_JWT"
```

## When something goes wrong

See `runtime_patch_kit/sovereign/DEPLOY.md` for the troubleshooting matrix.

## Questions for the operator

1. Which host runs Camaro?
2. systemd or Docker?
3. Run the synthetic stub for 24h, or wire the broker feed immediately?
