"""
Alpaca orphan-fill ingester — one-shot.
=======================================

Catches broker fills that MC never issued. Writes each into Mongo so
the Memory Kernel can classify them as UV (no internal receipt =
unverified, never trainable), with a permanent quarantine record of
"who fired this and from where".

Background:
    On 2026-05-18 the Alpaca paper account fired ~7 market orders
    (NVDA, MSFT, GOOGL) at 08:30 UTC with `source=access_key`. MC's
    `alpaca_audit_log` showed only disconnect events at the time —
    so the orders bypassed MC entirely. Probably a stale sidecar /
    cron / notebook holding a raw API key. This script captures
    every such order into MC's ledger so they're auditable forever.

Usage (locally; do NOT run in CI):

    cd /app/backend
    # Set Alpaca paper keys for the run — never commit them.
    export ALPACA_API_KEY_ID="PKxxxxxxxx"
    export ALPACA_API_SECRET_KEY="xxxxxxxx"
    # Optional: window. Defaults to all of 2026-05-18 UTC.
    export ALPACA_INGEST_AFTER="2026-05-18T00:00:00Z"
    export ALPACA_INGEST_UNTIL="2026-05-19T00:00:00Z"
    # Use paper by default; flip to live only if you know what you're
    # doing.
    export ALPACA_BASE_URL="https://paper-api.alpaca.markets"
    # MC admin JWT used to call the kernel submit endpoint.
    export MC_ADMIN_JWT="..."
    export MC_BASE_URL="http://localhost:8001"
    python -m scripts.alpaca_orphan_ingester

Output:
    For every filled order in the window:
      1. Insert into `broker_orders` with the broker_order_id
         (so SettlementOracle has a source to read from)
      2. POST to /api/admin/memory-kernel/submit with
         memory_type=execution, source_stack=alpaca_orphan
      3. Without a matching execution_receipts row, the kernel
         classifies it UV → quarantine row written with alert=CRITICAL

After running, every orphan is auditable forever and the kernel will
refuse to train on any of them.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from db import db


ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY_ID", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET_KEY", "")
ALPACA_AFTER = os.environ.get("ALPACA_INGEST_AFTER", "2026-05-18T00:00:00Z")
ALPACA_UNTIL = os.environ.get("ALPACA_INGEST_UNTIL", "2026-05-19T00:00:00Z")

MC_BASE_URL = os.environ.get("MC_BASE_URL", "http://localhost:8001")
MC_JWT = os.environ.get("MC_ADMIN_JWT", "")


def _die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(2)


async def _fetch_alpaca_filled_orders() -> List[Dict[str, Any]]:
    if not ALPACA_KEY or not ALPACA_SECRET:
        _die("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY missing in env.")

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    params = {
        "status": "closed",
        "after": ALPACA_AFTER,
        "until": ALPACA_UNTIL,
        "limit": "500",
        "direction": "asc",
    }
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.get(
            f"{ALPACA_BASE_URL}/v2/orders",
            headers=headers,
            params=params,
        )
        r.raise_for_status()
        orders = r.json()
    return [o for o in orders if o.get("status") == "filled"]


async def _insert_broker_order(order: Dict[str, Any]) -> str:
    """Write into the broker_orders collection (SettlementOracle source 1)."""
    broker_order_id = order["id"]
    doc = {
        "broker_order_id": broker_order_id,
        "symbol": order.get("symbol"),
        "status": "FILLED",
        "filled_qty": float(order.get("filled_qty") or 0),
        "filled_avg_price": float(order.get("filled_avg_price") or 0),
        "side": order.get("side"),
        "submitted_at": order.get("submitted_at"),
        "filled_at": order.get("filled_at"),
        "source": order.get("source", "access_key"),
        "venue": "alpaca",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "ingest_origin": "alpaca_orphan_ingester",
    }
    # Upsert so the script is re-runnable.
    await db.broker_orders.update_one(
        {"broker_order_id": broker_order_id},
        {"$set": doc},
        upsert=True,
    )
    return broker_order_id


async def _submit_to_kernel(order: Dict[str, Any]) -> Dict[str, Any]:
    """Submit the orphan as an execution memory. Kernel will classify UV
    because there is no matching execution_receipts row for this order."""
    if not MC_JWT:
        _die("MC_ADMIN_JWT missing in env.")

    payload = {
        "source_stack": "alpaca_orphan",
        "memory_type": "execution",
        "payload": {
            "symbol": order.get("symbol"),
            "broker_order_id": order["id"],
            # Deliberately omit execution_receipt_id — that's how the
            # kernel knows MC never issued this trade.
            "filled_qty": float(order.get("filled_qty") or 0),
            "filled_avg_price": float(order.get("filled_avg_price") or 0),
            "side": order.get("side"),
            "submitted_at": order.get("submitted_at"),
            "filled_at": order.get("filled_at"),
            "alpaca_source": order.get("source", "access_key"),
            "ingest_note": "Orphan fill — broker fired without MC receipt.",
        },
        "requested_provenance": None,
    }
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            f"{MC_BASE_URL}/api/admin/memory-kernel/submit",
            json=payload,
            headers={"Authorization": f"Bearer {MC_JWT}"},
        )
        r.raise_for_status()
        return r.json()


async def main() -> None:
    print(f"Fetching Alpaca filled orders {ALPACA_AFTER} → {ALPACA_UNTIL} …")
    orders = await _fetch_alpaca_filled_orders()
    print(f"  → {len(orders)} filled orders in window.")
    if not orders:
        return

    print()
    print(f"{'symbol':<8} {'side':<5} {'qty':>14} {'price':>10}  "
          f"{'submitted_at':<28} {'broker_order_id'}")
    classified: Dict[str, int] = {"VE": 0, "SO": 0, "DI": 0, "UV": 0}
    for o in orders:
        bid = await _insert_broker_order(o)
        result = await _submit_to_kernel(o)
        prov = result.get("provenance", "?")
        classified[prov] = classified.get(prov, 0) + 1
        print(
            f"{(o.get('symbol') or ''):<8} "
            f"{(o.get('side') or ''):<5} "
            f"{float(o.get('filled_qty') or 0):>14.8f} "
            f"{float(o.get('filled_avg_price') or 0):>10.2f}  "
            f"{(o.get('submitted_at') or ''):<28} "
            f"{bid}  → {prov}"
        )

    print()
    print("Kernel classification summary:")
    for k, v in classified.items():
        print(f"  {k}: {v}")
    print()
    print("Quarantine alerts written to `memory_kernel_quarantine`.")
    print("Use `/api/admin/memory-kernel/health` to verify the kernel is live.")


if __name__ == "__main__":
    asyncio.run(main())
