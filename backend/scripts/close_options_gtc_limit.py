"""Close the 7 SPY Dec-18 option legs that Alpaca refused as market
orders outside RTH. Uses GTC limit orders at current mark ± 5%
(slack ensures execution at Monday's open even if quote drifts).

All closes are audit-logged into broker_force_close_log so the trail
matches the rest of the operator force-close work.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx                                                      # noqa: E402
from db import db                                                 # noqa: E402
from shared.runtime.platform_survival import (                    # noqa: E402
    MCExecutionReceipt, policy_hash,
)


ALPACA_BASE = "https://paper-api.alpaca.markets"
ACTOR = "admin@risedual.io"
REASON = (
    "post_orphan_audit_2026_05_23_cleanup — closing 7 SPY Dec-18 option "
    "legs via GTC limit orders (market orders outside RTH refused by Alpaca)"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round_cents(v: float, *, up: bool) -> float:
    """Options need 2-decimal price; round through-the-mark to ensure fill."""
    # Alpaca options min tick is $0.01 below $3, $0.05 above. Use $0.05 to be safe.
    tick = 0.05 if v >= 3.0 else 0.01
    n = v / tick
    return round((int(n) + (1 if up else 0)) * tick, 2)


def _mint(symbol: str, side: str) -> dict:
    receipt = MCExecutionReceipt(
        accepted=True,
        final_verdict="OPERATOR_FORCED_CLOSE",
        reason="POST_BYPASS_AUDIT_CLEANUP_GTC_LIMIT",
        lane="options",
        symbol=symbol,
        direction=side.upper(),
        confidence=1.0,
        mc_policy_hash=policy_hash(),
        issued_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    secret = os.environ.get("RISEDUAL_MC_RECEIPT_SECRET", "")
    signed = receipt.sign(secret) if secret else receipt
    return asdict(signed)


async def main() -> None:
    key = os.environ["ALPACA_INGEST_KEY_ID"]
    sec = os.environ["ALPACA_INGEST_SECRET_KEY"]
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}

    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.get(f"{ALPACA_BASE}/v2/positions", headers=headers)
        r.raise_for_status()
        positions = [p for p in r.json() if p.get("asset_class") == "us_option"]

        if not positions:
            print("No option positions remaining.")
            return

        print(f"Closing {len(positions)} option positions via GTC limit @ mark ± 5%")
        results = []
        for p in positions:
            symbol = p["symbol"]
            qty = float(p["qty"])
            is_long = p.get("side") == "long"
            close_side = "sell" if is_long else "buy"
            # Mark per contract: market_value / qty. Per-share = /100.
            mv = float(p.get("market_value") or 0)
            mark_per_share = abs(mv / qty) / 100.0 if qty else 0.0
            # Slack 5% through the mark to guarantee fill at open.
            if close_side == "sell":
                limit = _round_cents(mark_per_share * 0.95, up=False)
            else:
                limit = _round_cents(mark_per_share * 1.05, up=True)

            client_order_id = f"opclose-{uuid.uuid4().hex[:12]}"
            body = {
                "symbol": symbol,
                "qty": str(abs(qty)),
                "side": close_side,
                "type": "limit",
                "limit_price": str(limit),
                "time_in_force": "gtc",
                "client_order_id": client_order_id,
                "position_intent": "sell_to_close" if close_side == "sell" else "buy_to_close",
            }
            rr = await cli.post(f"{ALPACA_BASE}/v2/orders", headers=headers, json=body)
            ok = rr.status_code in (200, 201)
            resp = rr.json() if ok else rr.text[:500]
            print(
                f"  {'OK ' if ok else 'ERR'} {symbol:<22} "
                f"{close_side:<4} qty={abs(qty):<5} limit=${limit:>7.2f} "
                f"(mark=${mark_per_share:.2f})"
            )
            if not ok:
                print(f"     -> {resp}")

            receipt = _mint(symbol, close_side)
            await db.broker_force_close_log.insert_one({
                "ts": _now(),
                "action": "OPERATOR_FORCED_CLOSE",
                "actor": ACTOR,
                "reason": REASON,
                "symbol": symbol,
                "qty": abs(qty),
                "side": close_side,
                "order_type": "limit_gtc",
                "limit_price": limit,
                "mark_at_submit": mark_per_share,
                "receipt_signature": receipt.get("signature"),
                "receipt_policy_hash": receipt.get("mc_policy_hash"),
                "broker_ok": ok,
                "broker_status_code": rr.status_code,
                "broker_response": resp,
                "freeze_was_on": True,
                "freeze_override": True,
            })
            results.append({"symbol": symbol, "ok": ok, "status": rr.status_code})

        print()
        oks = sum(1 for r in results if r["ok"])
        print(f"Done. accepted={oks}/{len(results)}")
        for r in results:
            if not r["ok"]:
                print(f"  failed: {r}")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
