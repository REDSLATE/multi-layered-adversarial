# RISEDUAL Mission Control — Backend Test Suite

## Tripwire suite

The `tripwire` pytest marker pins the **locked contracts** the council
and auto-router refactors stand on. If you edit any of:

- `shared/council.py`
- `shared/auto_router.py`
- `shared/execution.py` (specifically `/api/admin/execution/diagnose`)
- `shared/quantum_state.py`

…run the tripwire suite **before** you commit:

```bash
cd /app/backend
python -m pytest -m tripwire -v
```

You should see **65 passed**. If anything fails, the change touched
the locked surface. Make a conscious decision:

1. **Intentional doctrine change** → update the affected fixture(s),
   add an entry to `/app/memory/PRD.md` so the next session knows
   the contract shifted, then re-run `-m tripwire` until green.
2. **Unintentional** → roll the edit back. The tripwire saved you.

## What the tripwire pins

| File | Tests | Surface |
|---|---|---|
| `test_governance_verdict.py` | 10 | Council verdict-code matrix (all 8 codes) |
| `test_council_helpers.py` | 26 | Extracted pure helpers (opposes_direction, opponent_payload, compose_size, build_gate, quantum_opinions) |
| `test_council_diagnose_contract.py` | 11 | **Live HTTP shape of `/api/admin/execution/diagnose`** — top-level keys, gate-chain ordering, council gate required keys, quantum_state regime_probs invariants, broker block, first_blocker consistency |
| `test_auto_router_helpers.py` | 18 | Auto-router pure helpers (lane clamp, side mapping, effective_notional, blocked_response, build_receipt) |

## Running the rest

```bash
# everything (slow)
python -m pytest

# everything except the tripwire
python -m pytest -m "not tripwire"

# unit-only (skip the HTTP-integration tests)
python -m pytest --ignore=tests/test_council_diagnose_contract.py \
                  --ignore=tests/test_alpaca_execution_pipeline.py
```

## Pre-existing test failures (known stale)

Some non-tripwire tests assert mock-broker fixture state that drifts
with seat assignments / live DB state. They are **not regressions** —
they fail when the preview DB doesn't match the test's expected
fixture. Don't chase them as part of a refactor pass.
