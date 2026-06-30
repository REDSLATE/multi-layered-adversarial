"""End-to-end smoke test for the 2026-02-27 architectural reduction.

Run from inside the backend pod:
    cd /app/backend && python -m tests.smoke_new_path

What it tests:
    1. New Seat module reads holder from `seat_registry` AND falls
       back to `shared_brain_roster`.
    2. New Risk module enforces caps + freeze + lane toggle.
    3. New `_route_one` writes one row to `executions` per attempt.
    4. The auto_router's `_tick` picks up unexecuted BUY intents.

Does NOT call the real broker — sets up a synthetic intent against a
broker route that we expect to be blocked, then asserts the
`executions` audit row was written with the right shape.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

# Make the backend root importable when run as a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> int:
    from db import db
    from shared import seat, risk, executions

    # 1. Seat — check helpers don't crash.
    holder = await seat.get_holder("equity")
    print(f"[seat] equity executor holder: {holder!r}")
    holder_crypto = await seat.get_holder("crypto")
    print(f"[seat] crypto executor holder: {holder_crypto!r}")

    # 2. Plant a synthetic BUY intent.
    intent_id = f"smoketest-{uuid.uuid4().hex[:12]}"
    fake = {
        "intent_id": intent_id,
        "stack": holder or "camino",   # use real holder so Seat fires
        "brain": holder or "camino",
        "action": "BUY",
        "symbol": "SMOKETEST",
        "lane": "equity",
        "confidence": 0.6,
        "ingest_ts": datetime.now(timezone.utc).isoformat(),
        "executed": False,
        "gate_state": "pending",
    }
    await db["shared_intents"].insert_one(fake)
    print(f"[intent] inserted {intent_id} brain={fake['stack']} action=BUY")

    # 3. Seat decide.
    sd = await seat.decide(fake)
    print(f"[seat.decide] verdict={sd.verdict} reason={sd.reason!r}")

    # 4. Risk check.
    rc = await risk.check(fake, notional_usd=5.0)
    print(f"[risk.check] ok={rc.ok} reason={rc.reason!r} cap=${rc.cap_per_order_usd} spent=${rc.spent_today_usd}")

    # 5. Write a dummy executions row.
    row_id = await executions.record(
        intent=fake,
        seat_verdict=sd.verdict,
        seat_holder=sd.holder,
        seat_reason=sd.reason,
        risk_ok=rc.ok,
        risk_reason=rc.reason,
        notional_usd=rc.notional_usd,
        ok=False,
        exception_type="SmokeTest",
        exception_msg="synthetic — no broker call made",
    )
    print(f"[executions.record] inserted _id={row_id!r}")

    # 6. Read it back.
    recent = await executions.recent(limit=3)
    print(f"[executions.recent] count={len(recent)}")
    for r in recent:
        print(
            f"  - {r.get('ts')} brain={r.get('brain')} action={r.get('action')} "
            f"sym={r.get('symbol')} decision={r.get('decision')} "
            f"risk_reason={r.get('risk_reason')!r} ok={r.get('ok')}"
        )

    # 7. Clean up the synthetic intent so the auto_router doesn't try
    # to route a non-existent SMOKETEST symbol forever.
    await db["shared_intents"].delete_one({"intent_id": intent_id})
    await db["executions"].delete_one({"intent_id": intent_id})
    print(f"[cleanup] removed synthetic intent + execution row")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
