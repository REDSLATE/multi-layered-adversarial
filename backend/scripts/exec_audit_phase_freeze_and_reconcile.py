"""One-shot — execute the operator's 6-step audit response (2026-05-23).

Steps performed in order:
  1) FREEZE broker execution (explicit row in `broker_freeze_state`).
  2) (no-op here — external Alpaca fetch is the operator's call via
     POST /api/admin/alpaca/ingest-orphans-batch).
  3) (no-op here — same.)
  4) RECONCILE the 500 existing `broker_orders` rows against
     `execution_receipts` and `shared_intents`.
  5) MARK every unmatched row as UNVERIFIED_BROKER_EXECUTION in
     `broker_orders` and `memory_kernel_quarantine`.
  6) (code-level: enforced by adapter & router patches in this commit;
     no DB work needed.)

Usage:
    cd /app/backend
    python -m scripts.exec_audit_phase_freeze_and_reconcile
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from db import db                                             # noqa: E402
from shared.broker_freeze import freeze     # noqa: E402


ACTOR = os.environ.get("AUDIT_ACTOR", "admin@risedual.io")
FREEZE_REASON = os.environ.get(
    "AUDIT_REASON",
    "post_orphan_audit_2026_05_23 — investigating ~500 bypass fills; "
    "broker freeze stays ON until reconcile + adapter patches verified.",
)


async def step_1_freeze() -> dict:
    print("Step 1: freezing broker execution …")
    state = await freeze(FREEZE_REASON, ACTOR)
    print(f"  → frozen={state['frozen']} by={state['frozen_by']} reason={state['reason']}")
    return state


async def step_4_reconcile() -> dict:
    """Inline reconciliation against broker_orders (avoid HTTP loop)."""
    from routes.broker_reconcile_routes import (
        _persist_reconciliation, _reconcile_one, UNVERIFIED,
    )
    print("Step 4: reconciling broker_orders against execution_receipts …")
    counts = {"VERIFIED_MC_EXECUTION": 0, UNVERIFIED: 0, "errors": 0, "total": 0}
    async for order in db.broker_orders.find({}, {"_id": 0}):
        counts["total"] += 1
        bid = order.get("broker_order_id")
        if not bid:
            counts["errors"] += 1
            continue
        try:
            cls = await _reconcile_one(order)
            await _persist_reconciliation(bid, cls)
            counts[cls["provenance"]] = counts.get(cls["provenance"], 0) + 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! reconcile failed {bid}: {e!r}")
            counts["errors"] += 1
    print(f"  → counts={counts}")
    return counts


async def main() -> None:
    s1 = await step_1_freeze()
    s4 = await step_4_reconcile()
    print()
    print("=" * 60)
    print("AUDIT PHASE — final state")
    print("=" * 60)
    print(f"  freeze: {s1}")
    print(f"  reconcile counts: {s4}")
    print()
    print("Next operator actions:")
    print(" - POST /api/admin/alpaca/ingest-orphans-batch")
    print("   body: {windows: [{after:'2026-04-25T00:00:00Z', until:'2026-04-30T23:59:59Z'},")
    print("                    {after:'2026-05-04T00:00:00Z', until:'2026-05-18T23:59:59Z'}],")
    print("           dry_run:false}")
    print(" - Re-run reconcile after batch ingest.")
    print(" - POST /api/admin/broker/thaw  (only after all unverified are accounted for).")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
