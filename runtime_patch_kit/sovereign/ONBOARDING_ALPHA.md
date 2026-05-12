# Onboarding Packet — ALPHA brain

> **Hand this to the agent / operator working on the Alpha brain host.**
> Self-contained: contains everything needed to connect Alpha to Mission Control.

## Identity

- **Brain name**: `alpha`
- **Personality**: Trend follower — biased toward following established direction.
- **Default seat (today)**: Decider. Earns the strategist role in the War Room view.
- **Mode for first run**: `DTD` (deterministic training data — safe to learn weights).

## Mission Control connection details

```bash
MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
ALPHA_INGEST_TOKEN="alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55"
```

> ⚠️ The `MC_BASE_URL` above is the preview URL; when MC goes to prod, the operator
> updates one env var on the host — no code change needed.

## Suggested initial weights (Alpha — trend bias)

When you first create the brain's state file, seed it with these weights instead of
the kit's default symmetric `0.5, 0.5, 0.5`. This is what makes Alpha *Alpha* and
not a clone of Camaro:

```python
# Trend follower — heavy on trend, light on mean-reversion (RSI)
ALPHA_INITIAL_WEIGHTS = {
    "trend": 0.85,
    "macd":  0.65,
    "rsi":  -0.25,    # negative weight on RSI = "don't get tempted by oversold"
}
```

In code:
```python
from runtime_patch_kit.sovereign.sidecar import SovereignSidecar
from runtime_patch_kit.sovereign.local_state import LocalState

# On first run only — once the state file exists, this is a no-op.
state = LocalState(brain="alpha", mode="DTD")
if not state.weights:
    state.set_weights(ALPHA_INITIAL_WEIGHTS)
    state.set_learning_rate(0.06)
    state.save()
```

## Quick start (host machine, 5 minutes)

```bash
# 1. Copy the sovereign kit to this host:
#    (option A) git clone the MC repo; cd runtime_patch_kit/sovereign
#    (option B) scp -r runtime_patch_kit/sovereign you@host:~/sovereign && cd ~/sovereign

# 2. Confirm Python 3.11+
python3 -V

# 3. Set the env vars (use the values above)
export MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
export ALPHA_INGEST_TOKEN="alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55"

# 4. Verify doctrine locks before connecting (no MC needed)
python3 smoke_test.py     # expect 8/8 PASS

# 5. Start the sidecar
python3 sidecar.py --brain alpha --mode DTD --symbols BTC/USD ETH/USD --interval 60
```

Within ~60 seconds MC will show Alpha in `GET /api/admin/sovereign/state` and the
operator's `/runtime/alpha` page will render the Sovereign State tile.

## Suggested symbols for Alpha

Alpha's trend bias favors high-momentum names. Sensible defaults:

- **Crypto**: `BTC/USD ETH/USD SOL/USD`
- **Equities** (when feeders ship US-stock bars): `NVDA TSLA AMD`

Pass via `--symbols BTC/USD ETH/USD SOL/USD`.

## Doctrine reminders (non-negotiable)

1. `LIVE_TRADING_ENABLED` must stay False in `wild_adaptive_core_v2.py`.
   The sidecar refuses to start otherwise. MC also refuses any contribution
   payload with `live_trading_enabled=true` (HTTP 422).
2. Never write directly to MC's database. The only legal channels are the three
   MC HTTP endpoints used by `mc_client.py` (stance / contribution / heartbeat).
3. PRD mode disallows training. If Alpha is reading live data, send no
   `training_signal=true` payloads. To learn, switch to DTD mode and replay
   historical bars.

## Verifying from MC's side

Once Alpha is running, the operator can check:

```bash
curl -s "$MC_BASE_URL/api/admin/sovereign/state/alpha" \
  -H "Authorization: Bearer $OPERATOR_JWT"
```

Should return Alpha's current weights, mode, learning rate, recent outcomes, and
the seat-policy snapshot. Sovereign State tile on `/runtime/alpha` shows the
same.

## When something goes wrong

See `runtime_patch_kit/sovereign/DEPLOY.md` for the troubleshooting matrix and
systemd / Docker recipes for running long-term.

## Questions for the operator

If you're the agent reading this, send these back to the operator before
deploying:

1. Which host should Alpha live on?
2. Should I run as a systemd service or a Docker container?
3. Any non-default symbols you want Alpha watching?
4. Should the broker feed be wired now, or run with the synthetic stub for the
   first 24h to verify the connection works?
