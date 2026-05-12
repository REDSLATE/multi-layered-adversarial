# Onboarding Packet — REDEYE brain

> **Hand this to the agent / operator working on the REDEYE brain host.**
> Self-contained: contains everything needed to connect REDEYE to Mission Control.

## Identity

- **Brain name**: `redeye`
- **Personality**: Contrarian / opponent — explicitly biased toward the short side
  and toward calling out crowded trades.
- **Default seat (today)**: Opponent. Holds the adversarial dissent role in the
  War Room view.
- **Mode for first run**: `DTD` (deterministic training data — safe to learn weights).

## Mission Control connection details

```bash
MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
REDEYE_INGEST_TOKEN="redeye-ingest-9f3e7c1b-8d4a-4b6e-a2f5-1c9e3b7d4a82"
```

> ⚠️ The `MC_BASE_URL` above is the preview URL; when MC goes to prod, the operator
> updates one env var on the host — no code change needed.

## Suggested initial weights (REDEYE — contrarian / short bias)

```python
# Contrarian — bias toward fading crowded trades
REDEYE_INITIAL_WEIGHTS = {
    "trend": -0.70,    # heavy negative weight = lean opposite to the trend
    "macd":  -0.30,
    "rsi":    0.55,    # use RSI extremes as confirmation of the fade
}
```

In code:

```python
from runtime_patch_kit.sovereign.local_state import LocalState
state = LocalState(brain="redeye", mode="DTD")
if not state.weights:
    state.set_weights({"trend": -0.70, "macd": -0.30, "rsi": 0.55})
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
export REDEYE_INGEST_TOKEN="redeye-ingest-9f3e7c1b-8d4a-4b6e-a2f5-1c9e3b7d4a82"

# 4. Verify doctrine locks before connecting (no MC needed)
python3 smoke_test.py     # expect 8/8 PASS

# 5. Start the sidecar
python3 sidecar.py --brain redeye --mode DTD --symbols BTC/USD ETH/USD --interval 60
```

Within ~60 seconds MC will show REDEYE in `GET /api/admin/sovereign/state` and the
operator's `/runtime/redeye` page will render the Sovereign State tile.

## Suggested symbols for REDEYE

REDEYE looks for crowded trades and fade opportunities. Best on names with active
options markets and momentum-chaser behavior:

- **Crypto**: `BTC/USD ETH/USD` (deep enough liquidity to fade)
- **Equities** (when feeders ship): `TSLA NVDA GME AMC` (meme / high-beta names)

## Doctrine reminders (non-negotiable)

1. `LIVE_TRADING_ENABLED` must stay False in `wild_adaptive_core_v2.py`.
2. Never write directly to MC's DB — only `mc_client.py` HTTP endpoints.
3. PRD mode disallows training. To learn, switch to DTD and replay historical bars.

## Why REDEYE is the most important brain to keep honest

The opponent seat is where adversarial blindness comes from. If REDEYE doesn't
dissent on anything, the system has no devil's advocate and operators are
flying blind. MC's quorum-state tracking specifically flags
`adversarial_blindness=true` when the opponent seat is silent on an open
position — those flags only mean something if REDEYE is actually trying to find
fault.

The negative trend weights ARE the point. Don't tune them away to "improve"
performance, because performance for an opponent is measured by **catching
losers**, not by being right alongside everyone else.

## Verifying from MC's side

```bash
curl -s "$MC_BASE_URL/api/admin/sovereign/state/redeye" \
  -H "Authorization: Bearer $OPERATOR_JWT"
```

## When something goes wrong

See `runtime_patch_kit/sovereign/DEPLOY.md` for the troubleshooting matrix.

## Note on REDEYE's history

REDEYE was previously a sidecar advisor; it was promoted to a full seat in 2026-02.
The promotion gave it equal contribution rights to Alpha / Camaro / Chevelle. The
brain is new enough that its initial weights are essentially a working hypothesis
— expect them to drift meaningfully during the first month of DTD replay.

## Questions for the operator

1. Which host runs REDEYE?
2. systemd or Docker?
3. Run the synthetic stub for 24h, or wire the broker feed immediately?
4. Should REDEYE have a smaller symbol list than the others (it's the newest brain,
   might be worth limiting blast radius initially)?
