# Sovereign Sidecar — Drop-in Brain Wrapper

This folder is a self-contained kit for running any of the four
RISEDUAL brains (`alpha` / `camaro` / `chevelle` / `redeye`) as a
deterministic sovereign sidecar. Same intelligence layer, four
initializations, four personalities.

## Files

| File | What it is |
|---|---|
| `wild_adaptive_core_v2.py` | The operator's deterministic AI core (patched for RISEDUAL doctrine — see in-file header). |
| `local_state.py` | JSON-on-disk persistence of weights + decision log. Brain never writes to MC's DB. |
| `mc_client.py` | Stdlib HTTP client that POSTs stances + contribution snapshots to MC. |
| `sidecar.py` | Long-lived runner. `python sidecar.py --brain alpha --mode DTD`. |
| `STATE_SCHEMA.md` | Wire-format spec for the local file and contribution snapshot. |
| `smoke_test.py` | Doctrinal smoke tests — runnable on the brain host with no MC connection. |

## Doctrine (seat-governed; MC regulates at the execution gate)

The brain proposes; **MC is the regulator**. There is no
brain-side observation-only lock. Three things are still true:

1. **Brain core** ships intents to MC. `execute_trade()` builds an
   intent envelope; the brain hands it to MC via `MCClient` and lets
   MC's seat-policy + execution-gate chain decide whether to route
   to a broker.
2. **Sidecar** logs the brain's declared `LIVE_TRADING_ENABLED`
   posture on startup but does NOT refuse to start. Refusing to
   start was the old observation-only doctrine; it's been removed.
3. **Mission Control API** accepts any `live_trading_enabled` value
   the brain declares. MC observes; the seat policy at the
   execution gate is the only authority on what fires.

The brain talks to MC via **three endpoints only**:
- `POST /api/runtime-discussion/positions/{id}/stance` — vote on a position.
- `POST /api/runtime-discussion/sovereign/contribution` — periodic state snapshot.
- `POST /api/heartbeat-ping/{brain}` — liveness ping.

Direct DB writes to MC's MongoDB are **never** done by the brain. The
brain's own MongoDB / SQLite / JSON state is unrelated.

## DTD vs PRD modes

| Mode | What it means | MC's behavior |
|---|---|---|
| **DTD** | Brain is reading historical / labeled / replay bars. Weight updates expected. | Accepts `training_signal=true`; accepts snapshots. |
| **PRD** | Brain is reading live market data. | Accepts snapshots AND `training_signal=true`. MC observes; downstream consumers decide what to do with the brain's claim. |

Switch via `--mode DTD` or `--mode PRD` on the sidecar. The mode is
stored locally and ships on every contribution.

## Required env vars (on the brain host)

```bash
# One per brain — must match MC's `<BRAIN>_INGEST_TOKEN`.
export ALPHA_INGEST_TOKEN="..."
# (or CAMARO_INGEST_TOKEN / CHEVELLE_INGEST_TOKEN / REDEYE_INGEST_TOKEN)

# MC's external base URL.
export MC_BASE_URL="https://mc.example.com"

# Optional: override the on-disk state path.
export SOVEREIGN_STATE_PATH="$HOME/.risedual/alpha/state.json"

# Optional: cap on local decision-log size (default 5000).
export SOVEREIGN_LOG_MAX="10000"
```

## Quick start

```bash
# On the alpha brain host:
cd runtime_patch_kit/sovereign
python sidecar.py --brain alpha --mode DTD --symbols BTC/USD ETH/USD --interval 60
```

The sidecar will:
1. Load (or create) `~/.risedual/alpha/state.json`.
2. Every 60s, run the deterministic core on each symbol, persist the
   decision, and POST a contribution snapshot to MC.
3. Send heartbeats so MC's staleness alerts know the brain is alive.

## Connecting to a real market feed

The shipped sidecar uses a synthetic top-of-book stub so it can dry-run
without any broker connection. Replace it by subclassing
`SovereignSidecar` (in your own host repo, NOT in this kit) and
overriding `read_top`:

```python
from runtime_patch_kit.sovereign.sidecar import SovereignSidecar

class AlphaSidecar(SovereignSidecar):
    def __init__(self, **kw):
        super().__init__(**kw, top_of_book_fn=self.read_kraken_top)

    def read_kraken_top(self, symbol):
        # call your existing Kraken poller / cache
        return {"symbol": symbol, "price": ..., "technicals": {...}}
```

## Verifying the doctrine locally

Run the smoke tests on the brain host (no MC needed):

```bash
python smoke_test.py
```

Expected: 8 / 8 PASS, including the "refuse to start if
LIVE_TRADING_ENABLED gets flipped" check.

## Where MC stores the contributions

- Latest snapshot per brain: `sovereign_state` collection.
- Immutable history: `sovereign_state_history` (every contribution).
- Operator audit timeline: `sovereign_audit_log`.

Frontend tile lives on `/runtime/{brain}` and reads
`GET /api/admin/sovereign/state/{brain}` (operator JWT).

## What this kit is NOT

- It does **not** install on Mission Control's server. This folder is
  copied to **brain hosts**.
- It does **not** decide *which* position to vote on — that's the
  operator's job (or a future `active_position_resolver` plugged into
  the sidecar).
- It does **not** route orders to a broker on its own. The
  `execute_trade()` helper builds an intent envelope; the brain hands
  it to MC. **MC's seat policy + execution-gate chain decides whether
  the order routes to a broker** — that's the regulator. The brain
  has no regulatory authority over its own intents.
