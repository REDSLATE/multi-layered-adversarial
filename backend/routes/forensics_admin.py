"""Closed-trade forensics admin (2026-08-05 operator directive)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/forensics", tags=["forensics"])


@router.get("/closed-trades")
async def closed_trades(
    since: Optional[str] = None,
    with_bars: bool = True,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.forensics.trade_forensics import (  # noqa: WPS433
        DEFAULT_SINCE, closed_trade_report,
    )
    report = await closed_trade_report(db, since or DEFAULT_SINCE,
                                       with_bars=with_bars)
    await db["forensic_reports"].update_one(
        {"_id": f"closed-trades-{report['since'][:10]}"},
        {"$set": report}, upsert=True)
    return {"ok": True, **report}


@router.get("/entry-latency")
async def entry_latency(
    n: int = 50,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.forensics.trade_forensics import entry_latency_report  # noqa: WPS433
    return {"ok": True, **await entry_latency_report(db, n)}


@router.get("/broker-report")
async def broker_report_route(
    start: Optional[str] = None,
    end: Optional[str] = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Webull-sourced forensics — reconstructs round trips from the
    broker's own fill history. Works even when the internal receipts
    database is empty (2026-08: it always was)."""
    from shared.forensics.broker_forensics import broker_report  # noqa: WPS433
    report = await broker_report(db, start=start, end=end)
    if report.get("ok"):
        await db["forensic_reports"].update_one(
            {"_id": f"broker-webull-{report['generated_at'][:10]}"},
            {"$set": report}, upsert=True)
    return report


class BrokerActuals(BaseModel):
    months: Dict[str, float]  # {"2026-06": -28.01, ...}


@router.post("/broker-actuals")
async def broker_actuals(
    body: BrokerActuals,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.forensics.trade_forensics import BROKER_ACTUALS_ID  # noqa: WPS433
    existing = await db["runtime_flags"].find_one(
        {"_id": BROKER_ACTUALS_ID}, {"months": 1}, max_time_ms=3000) or {}
    months = {**(existing.get("months") or {}), **body.months}
    await db["runtime_flags"].update_one(
        {"_id": BROKER_ACTUALS_ID},
        {"$set": {"months": months,
                  "updated_at": datetime.now(timezone.utc).isoformat(),
                  "updated_by": user.get("email") or "operator"}},
        upsert=True)
    return {"ok": True, "months": months}
