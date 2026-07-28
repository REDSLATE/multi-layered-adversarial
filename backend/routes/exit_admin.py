"""Exit Monitor admin — policy knobs, plan visibility, CLOSE NOW."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.exits import monitor as exit_monitor
from shared.exits.policy import get_policy, set_policy

router = APIRouter(prefix="/admin/exits", tags=["exit-monitor"])


@router.get("")
async def exits_overview(_user: dict = Depends(get_current_user)):  # noqa: B008
    # Live plans come from the hot-path store (memory + SQLite) —
    # real-time and Atlas-independent (2026-07-23 P0 #2 migration).
    from shared.hotpath import exit_plans as plan_store  # noqa: WPS433
    plans = sorted(
        plan_store.load_panel(),
        key=lambda p: p.get("adopted_at") or "", reverse=True,
    )[:100]
    recent = []
    async for r in db[exit_monitor.EXIT_RECEIPTS].find(
        {}, {"_id": 0},
    ).sort("ts", -1).limit(20):
        recent.append(r)
    from shared.exits.outcomes import brain_scorecard
    return {
        "policy": await get_policy(),
        "monitor": exit_monitor.get_status(),
        "plans": plans,
        "recent_receipts": recent,
        "scorecard": await brain_scorecard(),
    }


@router.post("/policy")
async def update_policy(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    lane = (body.get("lane") or "").strip().lower()
    if lane not in ("equity", "crypto", "options"):
        raise HTTPException(status_code=422, detail="lane must be equity|crypto|options")
    fields: dict = {}
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    for k, lo, hi in (
        ("sl_pct", 0.1, 50.0), ("tp_pct", 0.1, 100.0), ("max_hold_h", 0.5, 720.0),
    ):
        if k in body and body[k] is not None:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{k} must be a number")
            if not (lo <= v <= hi):
                raise HTTPException(
                    status_code=422, detail=f"{k} out of range [{lo}, {hi}]",
                )
            fields[k] = v
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to update")
    policy = await set_policy(lane, fields, _user.get("email") or "unknown")
    return {"ok": True, "policy": policy}


@router.get("/diagnose")
async def diagnose_crypto(_user: dict = Depends(get_current_user)):  # noqa: B008
    """WHY ISN'T IT SELLING? — per-holding autopsy of the crypto exit
    chain (2026-07-28 operator report: 'It hasn't sold anything').

    For every Kraken holding: priced? adopted? entry basis + source,
    stop/target, distance to stop, plan status/attempts/last_error.
    FAIL-LOUD `findings` name every reason a holding cannot sell."""
    from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
    from shared.crypto.kraken import call_private  # noqa: WPS433
    from shared.hotpath import exit_plans as plan_store  # noqa: WPS433

    policy = await get_policy()
    status = exit_monitor.get_status()
    findings: list[str] = []

    if not status.get("running"):
        findings.append("EXIT MONITOR IS NOT RUNNING — nothing auto-sells.")
    if not policy["crypto"]["enabled"]:
        findings.append(
            "CRYPTO EXIT LANE IS OFF (exit_policy.crypto.enabled=false) — "
            "the monitor skips ALL crypto holdings every tick. Nothing "
            "will EVER auto-sell until you press 'crypto ARMED' in the "
            "Exit Monitor panel.")

    holdings: list[dict] = []
    broker_error = None
    try:
        adapter = await get_kraken_adapter()
        if adapter is None:
            raise RuntimeError("kraken adapter unavailable (no credentials)")
        balances = await call_private(
            "/0/private/Balance", adapter.public_key, adapter.private_key, {},
        )
    except Exception as exc:  # noqa: BLE001
        broker_error = str(exc)[:300]
        balances = {}
    if broker_error:
        findings.append(f"KRAKEN UNREACHABLE: {broker_error}")

    # ── Emission → seat flow (2026-07-28 operator fix #2) ─────────
    # Barracuda emitting while GTO holds crypto:executor = nothing
    # routes. Surface the seat holder vs who is actually emitting.
    flow: dict = {}
    try:
        from datetime import datetime, timedelta, timezone  # noqa: WPS433
        from shared.executor_seat import (  # noqa: WPS433
            get_seat_holder, seats_with_execute,
        )
        seats = seats_with_execute("crypto")
        holders = {s: await get_seat_holder(s) for s in seats}
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        emitters = await db["shared_intents"].distinct(
            "stack", {"lane": "crypto", "ingest_ts": {"$gte": since},
                      "audit_only": {"$ne": True}})
        flow = {"crypto_execute_seats": holders,
                "emitting_brains_24h": sorted(emitters)}
        try:
            from shared.risk_sizer.buy_allowlist import get_allowlist  # noqa: WPS433
            al = await get_allowlist()
            flow["buy_allowlist"] = {
                "enabled": al.get("enabled"),
                "size": len(al.get("symbols") or []),
                "symbols": al.get("symbols"),
            }
        except Exception:  # noqa: BLE001
            pass
        holder_set = {h for h in holders.values() if h}
        if emitters and holder_set and not (set(emitters) & holder_set):
            findings.append(
                f"SEAT MISMATCH: brains emitting crypto intents "
                f"({sorted(emitters)}) do NOT hold the crypto execute "
                f"seat ({holders}) — nothing routes. Assign the seat or "
                "have the seat-holder emit.")
    except Exception as exc:  # noqa: BLE001
        flow = {"error": str(exc)[:200]}

    plans_by_symbol = {
        p["symbol"]: dict(p) for p in plan_store.load_live("crypto")
    }
    for code, raw in (balances or {}).items():
        base = exit_monitor._normalize_kraken_asset(code)
        if base is None:
            continue
        try:
            qty = float(raw)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        symbol = f"{base}/USD"
        price = await exit_monitor._crypto_price(symbol)
        value = round(qty * price, 2) if price else None
        row: dict = {"symbol": symbol, "qty": qty, "price": price,
                     "value_usd": value}
        if price is None:
            row["blocker"] = "UNPRICEABLE — no Kraken ticker for this pair; the monitor silently skips it (never adopted, never sold)"
            findings.append(f"{symbol}: unpriceable → never adopted.")
            holdings.append(row)
            continue
        if value is not None and value < 1.0:
            row["blocker"] = "dust (<$1) — skipped by design"
            holdings.append(row)
            continue
        plan = plans_by_symbol.get(symbol)
        if plan is None:
            row["blocker"] = (
                "no exit plan — lane disabled or awaiting adoption "
                "(next tick)" if not policy["crypto"]["enabled"]
                else "no exit plan yet — adopts on next monitor tick (~20s)")
            holdings.append(row)
            continue
        entry = float(plan.get("entry_price") or 0)
        stop = float(plan.get("stop_price") or 0)
        row.update({
            "plan_id": plan.get("plan_id"),
            "plan_status": plan.get("status"),
            "entry_price": entry or None,
            "entry_source": plan.get("entry_source"),
            "stop_price": stop or None,
            "target_price": plan.get("target_price"),
            "levels_source": plan.get("levels_source"),
            "pnl_pct_vs_entry": round((price / entry - 1) * 100, 2) if entry else None,
            "pct_to_stop": round((price / stop - 1) * 100, 2) if stop else None,
            "max_hold_until": plan.get("max_hold_until"),
            "reanchor_attempted": plan.get("reanchor_attempted"),
            "attempts": plan.get("attempts"),
            "last_error": plan.get("last_error"),
            "exit_reason": plan.get("exit_reason"),
        })
        if plan.get("status") == "error":
            findings.append(
                f"{symbol}: exit FAILED {plan.get('attempts')}× and gave up "
                f"— last_error: {plan.get('last_error')}")
            row["blocker"] = "exit attempts exhausted — see last_error"
        elif stop and price <= stop and plan.get("status") == "active":
            findings.append(
                f"{symbol}: price is AT/BELOW its stop but the plan is "
                "still active — should trigger on the next tick; if it "
                "persists, check monitor errors.")
        elif entry and not plan.get("entry_source") and not plan.get("reanchor_attempted"):
            row["note"] = "legacy plan — entry basis will re-anchor to true cost on next tick"
        holdings.append(row)

    return {
        "ok": not findings,
        "findings": findings,
        "crypto_lane_enabled": policy["crypto"]["enabled"],
        "monitor_running": status.get("running"),
        "last_tick_at": status.get("last_tick_at"),
        "flow": flow,
        "holdings": holdings,
    }


@router.post("/close-now")
async def close_now(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    plan_id = (body.get("plan_id") or "").strip()
    if not plan_id:
        raise HTTPException(status_code=422, detail="plan_id required")
    result = await exit_monitor.close_now(plan_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "close failed")
    return result


@router.post("/run-once")
async def run_once(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "summary": await exit_monitor.run_once()}
