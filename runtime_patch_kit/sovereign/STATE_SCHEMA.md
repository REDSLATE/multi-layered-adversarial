# Sovereign Local-State Schema (v1)

Each sovereign brain persists exactly one JSON file on its own host. This
document is the contract: what fields exist, what they mean, who reads
them.

Default path: `~/.risedual/<brain>/state.json` (override via
`SOVEREIGN_STATE_PATH` env var). The file is the single source of truth
on the brain host; Mission Control never reads it.

## Top-level fields

| Field | Type | Required | Doctrine |
|---|---|---|---|
| `schema_version` | int | ✓ | Bumped on any format change. Current: `1`. |
| `brain` | str | ✓ | One of `alpha`/`camaro`/`chevelle`/`redeye`. |
| `mode` | str | ✓ | `DTD` (training/replay) or `PRD` (live observation). |
| `live_trading_enabled` | bool | ✓ | **MUST be `false`**. Reasserted false on every load. |
| `weights` | object | ✓ | Per-feature weights, bounded in `[-3, +3]`. ≤ 16 keys. |
| `learning_rate` | float | ✓ | Bounded `[0, 0.5]`. Applied only when `mode=DTD`. |
| `memory` | object | optional | Free-form dict the brain uses for cross-tick state. |
| `full_decision_log` | array | ✓ | All decisions the core has made. Capped at `SOVEREIGN_LOG_MAX` (5000). |
| `created_at` | str | ✓ | ISO 8601 UTC. |
| `updated_at` | str | ✓ | ISO 8601 UTC, refreshed on every save. |

## `full_decision_log[i]` shape (one decision)

This is the dataclass `AdaptiveDecision` from `wild_adaptive_core_v2.py`,
serialized via `dataclasses.asdict`:

```json
{
  "symbol": "BTC/USD",
  "action": "BUY",
  "confidence": 0.78,
  "notional": 0.0,
  "features": {"trend": 1.0, "macd": 1.0, "rsi": 0.0},
  "weights_snapshot": {"trend": 0.5, "macd": 0.5, "rsi": 0.5},
  "created_at": "2026-02-13T10:11:12.345678+00:00",
  "resolved": false,
  "confidence_origin": {"trend": 0.5, "macd": 0.5, "rsi": 0.0}
}
```

Resolved decisions (`resolved=true`) MAY also carry an `outcome` field
(`-1` / `0` / `+1`) that the operator wrote back from MC's outcome path.
Only resolved decisions are eligible to ship in the `recent_outcomes`
field of a contribution snapshot.

## Contribution snapshot (what gets shipped to MC each tick)

This is a strict subset of local state. MC stores it; the brain keeps
the full history.

```json
{
  "mode": "DTD",
  "live_trading_enabled": false,
  "weights": {"trend": 0.5, "macd": 0.5, "rsi": 0.5},
  "learning_rate": 0.05,
  "confidence_delta": 0.0,
  "delta_reason": "",
  "training_signal": false,
  "recent_outcomes": [/* up to 20 resolved decisions */],
  "notes": "tick @ 1707824400"
}
```

## What MC stores per brain

MC stores the latest snapshot in `sovereign_state` (one doc per brain)
and appends every contribution to `sovereign_state_history`. The
snapshot is augmented with the seat-policy snapshot at the time of
receipt:

```json
{
  "brain": "alpha",
  "mode": "DTD",
  "live_trading_enabled": false,
  "weights": {...},
  "learning_rate": 0.05,
  "training_signal": false,
  "confidence_delta": 0.0,          // BOUNDED at ±0.25 server-side
  "delta_reason": "",
  "recent_outcomes": [...],
  "notes": "...",
  "posted_as": "decider",            // seat at receipt time
  "seat_epoch": 7,
  "may_decide": true,
  "may_execute": false,
  "may_override": true,
  "may_veto": false,
  "updated_at": "..."
}
```

## Doctrine guarantees

1. **`live_trading_enabled` cannot be `true`** at any layer — brain
   core defaults `False`, sidecar reasserts on load, MC schema rejects
   `True` at the API.
2. **Confidence deltas are bounded** at `±0.25` server-side. Raw
   value + clamp flag preserved in `sovereign_state_history` so the
   operator can spot brains hammering the cap.
3. **PRD-mode brains cannot ship `training_signal=true`.** MC rejects
   with HTTP 422. To learn, the brain must enter DTD mode (replay
   against historical data).
4. **Seat policy is snapshotted on every contribution.** A brain's
   permissions on a contribution are whatever the seat it held at the
   moment of receipt allowed — not the seat it holds today.

## Migration path (future schema bumps)

Bump `SCHEMA_VERSION` in `local_state.py`. The loader currently accepts
any version (forward-compatible reads). Operators changing the schema
should provide a one-shot migrator that rewrites old state files in
place. MC's history collection retains older snapshots indefinitely
for replay.
