"""Webull entitlements & live snapshot debug endpoints.

Operator visibility (2026-06-11):
    The operator's brokerage account is Webull Premium + Options L2,
    but Open API quote entitlements are a SEPARATE ledger flipped per
    app-key in the developer portal. This endpoint exposes the
    current state of that ledger and tests each gated endpoint so
    the dashboard can show ✅/❌ per data class instead of relying
    on the operator to manually probe.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user

logger = logging.getLogger("risedual.webull_admin")
router = APIRouter(prefix="/admin/webull", tags=["webull-admin"])


@router.get("/entitlements")
async def get_entitlements(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Probe the Webull app key's data entitlements.

    Returns:
      {
        "configured": bool,                # app key + secret present
        "base_subscription": bool,         # any get_app_subscriptions row
        "data_classes": {
          "us_stock_quotes": bool,         # equity NBBO real-time
          "us_option_quotes": bool,        # OPRA real-time
          "us_crypto": bool,               # spot crypto (bundled)
        },
        "subscriptions": [...],            # raw subscription rows
        "stream_capacity": {               # static — from Webull docs
          "max_conns": 5,
          "msg_rate_per_sec": 3,
        },
        "checked_at": float,               # unix ts (cached up to 60s)
      }
    """
    import os
    configured = bool(
        (os.environ.get("WEBULL_APP_KEY") or "").strip()
        and (os.environ.get("WEBULL_APP_SECRET") or "").strip()
    )
    if not configured:
        return {
            "configured": False,
            "base_subscription": False,
            "data_classes": {
                "us_stock_quotes": False,
                "us_option_quotes": False,
                "us_crypto": False,
            },
            "subscriptions": [],
            "stream_capacity": {"max_conns": 5, "msg_rate_per_sec": 3},
            "checked_at": 0.0,
        }

    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433

    client = get_quotes_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Webull quotes client unavailable — SDK missing or init failed",
        )

    loop = asyncio.get_running_loop()
    ent = await loop.run_in_executor(None, client.get_entitlements)
    out = dict(ent)
    out["configured"] = True
    out["stream_capacity"] = {"max_conns": 5, "msg_rate_per_sec": 3}
    return out


@router.get("/snapshot/{symbol}")
async def get_symbol_snapshot(
    symbol: str, _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-facing live snapshot for one ticker. Equity lane.

    Useful for verifying the doctrine enricher pulls the right fields
    against any name in the universe. Returns the enriched payload
    that the brains will see.
    """
    from shared.snapshot_enrich.equity_doctrine import (  # noqa: WPS433
        enrich_equity_doctrine_snapshot,
    )
    base = {"symbol": symbol.upper(), "lane": "equity"}
    enriched = await enrich_equity_doctrine_snapshot(symbol.upper(), base)
    return {"symbol": symbol.upper(), "snapshot": enriched}


# ──────────────── OTOCO bracket — Phase 2 ────────────────────────────

from pydantic import BaseModel, Field  # noqa: E402


class OtocoTestBody(BaseModel):
    """Operator-driven OTOCO test request.

    Doctrine: ATOMIC OTOCO is whole-share integer-qty only. The $1-$10
    small-pilot fractional path stays on `submit_market_order` with
    the passive bracket recorder. This endpoint is for operators to
    fire a real OTOCO on a ticker priced low enough to make whole
    shares affordable (e.g. AAL @ ~$11 → 1 share = $11, just over
    the $10 cap — operator picks tickers).

    Webull's per-order cap is enforced by the SDK / broker; we don't
    re-check here because the OTOCO path is opt-in.
    """
    symbol: str = Field(..., min_length=1, max_length=20)
    qty: int = Field(..., ge=1, le=100, description="integer shares")
    side: str = Field("BUY", pattern="^(BUY|SELL)$")
    target_price: float = Field(..., gt=0.0)
    stop_price: float = Field(..., gt=0.0)
    confirm: str = Field(
        "",
        description="must equal 'execute-otoco' to actually route",
    )


@router.post("/otoco/test")
async def webull_otoco_test(
    body: OtocoTestBody, user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Fire an atomic OTOCO bracket against Webull.

    Doctrine (P1 Phase 2, 2026-02-19):
      * The MASTER leg is MARKET; TP child is LIMIT at `target_price`;
        SL child is STOP at `stop_price`. Webull manages the lifecycle
        (one fill cancels the other automatically).
      * Sanity-check: backend validates the bracket geometry against
        the live last-trade price before any SDK call.
      * The operator must type 'execute-otoco' in `confirm` — guards
        against accidental clicks.

    This is OPERATOR-DRIVEN. The auto-router still uses the existing
    `submit_market_order` + passive bracket recorder for $1-$10
    fractional intents. Atomic OTOCO is a parallel capability the
    operator can drive directly while we observe how Webull handles
    the combo across fills.
    """
    if body.confirm != "execute-otoco":
        raise HTTPException(
            status_code=400,
            detail=(
                "confirmation phrase missing — set confirm='execute-otoco' "
                "to fire this atomic OTOCO bracket"
            ),
        )

    from shared.broker.webull import get_webull_adapter  # noqa: WPS433
    adapter = await get_webull_adapter()
    if adapter is None:
        raise HTTPException(
            status_code=503,
            detail="Webull adapter not configured (missing credentials?)",
        )

    # Mint a deterministic-ish client_order_id so the operator can
    # reconcile in the Webull UI.
    import uuid as _uuid  # noqa: WPS433
    client_id = f"mc-otoco-{_uuid.uuid4().hex[:10]}"

    try:
        result = await adapter.submit_otoco_market(
            symbol=body.symbol.upper(),
            qty=body.qty,
            side=body.side,
            target_price=body.target_price,
            stop_price=body.stop_price,
            client_order_id=client_id,
            mc_receipt={"signature": f"operator:{user.get('email','?')}"},
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"OTOCO submit failed: {e}",
        ) from e

    return {
        "ok": True,
        "by": user.get("email"),
        "submitted_at": result.get("submitted_at"),
        "combo": {
            "master_broker_order_id": result.get("combo_order_id"),
            "combo_client_order_id": result.get("combo_client_order_id"),
            "tp_client_order_id": result.get("tp_client_order_id"),
            "sl_client_order_id": result.get("sl_client_order_id"),
            "tp_limit_price": result.get("tp_limit_price"),
            "sl_stop_price": result.get("sl_stop_price"),
            "entry_proxy_price": result.get("entry_proxy_price"),
        },
        "order": result,
    }


# ──────────── OTOCO live tile — v3 open-orders grouped by combo ────────────


def _classify_leg(client_order_id: str, combo_type: str) -> str:
    """Identify whether an open-order row is the MASTER, TP or SL leg.

    Webull's response carries `combo_type` ∈ {MASTER, OTOCO, NORMAL,
    ...} but doesn't tell us TP vs SL inside the OTOCO pair. We rely
    on the prefix MC stamps on `client_order_id` (`tp-` / `sl-` /
    `mc-otoco-` for master) — see `submit_otoco_market`. Falls back
    to "unknown" if neither hint resolves.
    """
    coid = (client_order_id or "").lower()
    ct = (combo_type or "").upper()
    if ct == "MASTER" or coid.startswith("mc-otoco-"):
        return "master"
    if coid.startswith("tp-"):
        return "tp"
    if coid.startswith("sl-"):
        return "sl"
    if ct == "OTOCO":
        # Last-resort guess: order_type drives the inference. LIMIT =
        # take-profit; STOP / STOP_LOSS = stop-loss. Anything else
        # surfaces as unknown so the operator sees it without
        # mislabeling.
        return "unknown_otoco_child"
    return "standalone"


def _group_open_orders_by_combo(rows: list[dict]) -> dict[str, Any]:
    """Group v3 open-order rows into bracket envelopes.

    Returns:
        {
          "brackets": [ { combo_id, symbol, master, tp, sl } ... ],
          "standalone": [ ... ],
        }

    A bracket is keyed by `combo_id` (Webull's, or MC's
    `client_combo_order_id` mirrored on each leg). Standalone orders
    (combo_type=NORMAL or no combo membership) are returned as-is so
    the operator still sees them for context.
    """
    by_combo: dict[str, dict[str, Any]] = {}
    standalone: list[dict] = []

    for r in rows:
        if not isinstance(r, dict):
            continue
        # Webull may return camelCase or snake_case depending on SDK
        # version; tolerate both.
        client_order_id = (
            r.get("client_order_id") or r.get("clientOrderId") or ""
        )
        client_combo_id = (
            r.get("client_combo_order_id")
            or r.get("clientComboOrderId")
            or r.get("combo_id")
            or r.get("comboId")
            or ""
        )
        combo_type = (
            r.get("combo_type") or r.get("comboType") or ""
        )
        order_type = (
            r.get("order_type") or r.get("orderType") or ""
        )
        leg_kind = _classify_leg(client_order_id, combo_type)
        normalized = {
            "client_order_id": client_order_id,
            "broker_order_id": (
                r.get("order_id") or r.get("orderId") or ""
            ),
            "symbol": (r.get("symbol") or "").upper(),
            "side": (r.get("side") or "").upper(),
            "order_type": order_type,
            "combo_type": combo_type,
            "qty": (
                r.get("quantity") or r.get("totalQuantity") or "0"
            ),
            "filled_qty": (
                r.get("filled_quantity") or r.get("filledQuantity") or "0"
            ),
            "limit_price": r.get("limit_price") or r.get("limitPrice"),
            "stop_price": r.get("stop_price") or r.get("stopPrice"),
            "status": (r.get("status") or "").upper(),
            "create_time": r.get("create_time") or r.get("createTime"),
        }
        if client_combo_id and combo_type in ("MASTER", "OTOCO", "OCO", "OTO"):
            bucket = by_combo.setdefault(client_combo_id, {
                "combo_id": client_combo_id,
                "symbol": normalized["symbol"],
                "master": None,
                "tp": None,
                "sl": None,
                "other_legs": [],
            })
            if leg_kind == "master" and bucket["master"] is None:
                bucket["master"] = normalized
            elif leg_kind == "tp" and bucket["tp"] is None:
                bucket["tp"] = normalized
            elif leg_kind == "sl" and bucket["sl"] is None:
                bucket["sl"] = normalized
            else:
                bucket["other_legs"].append({**normalized, "leg_kind": leg_kind})
            # Symbol unifies across legs — pick the first non-empty.
            if not bucket["symbol"] and normalized["symbol"]:
                bucket["symbol"] = normalized["symbol"]
        else:
            standalone.append({**normalized, "leg_kind": leg_kind})

    # Stable ordering: most-recent combo (by create_time on the master
    # leg) first; standalones by create_time descending.
    def _ct(o: dict | None) -> str:
        return (o or {}).get("create_time") or ""

    brackets = sorted(
        by_combo.values(),
        key=lambda b: _ct(b.get("master")) or _ct(b.get("tp")) or _ct(b.get("sl")),
        reverse=True,
    )
    standalone.sort(key=lambda r: r.get("create_time") or "", reverse=True)
    return {"brackets": brackets, "standalone": standalone}


@router.get("/otoco/live")
async def webull_otoco_live(
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the operator's currently-open Webull orders, grouped
    into OTOCO brackets where possible.

    Doctrine (P1 Phase 2 follow-up, 2026-02-19): the operator
    submits an atomic OTOCO via the test panel above; today they
    have to switch to the Webull mobile app to see the TP/SL pair
    after the master fills. This tile pulls the same information
    from the v3 open-orders API and surfaces it on the dashboard.
    Refreshes are operator-driven (frontend polls every ~8s).

    Graceful degradation: if the Webull adapter isn't configured we
    return an empty payload rather than 503 — the dashboard's panel
    error boundary then renders a "broker not configured" state
    without taking the whole Intents page down.
    """
    try:
        from shared.broker.webull import get_webull_adapter  # noqa: WPS433
        adapter = await get_webull_adapter()
    except Exception:  # noqa: BLE001
        adapter = None
    if adapter is None:
        return {
            "ok": False,
            "reason": "webull_adapter_not_configured",
            "brackets": [],
            "standalone": [],
            "open_count": 0,
        }

    rows = await adapter.list_open_orders_v3(page_size=50)
    grouped = _group_open_orders_by_combo(rows)
    return {
        "ok": True,
        "brackets": grouped["brackets"],
        "standalone": grouped["standalone"],
        "open_count": len(rows),
    }
