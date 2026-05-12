"""Public traffic verification — operator-only.

Two pieces:
  * `public_traffic_middleware` — logs every request to /api/public/*
    with endpoint, tier, status, latency, timestamp. Best-effort: never
    blocks the request even if Mongo is hiccupping.
  * `router` — operator-JWT endpoints to read the log and summary
    stats. Used by the frontend `/public-traffic` page during the
    risedual.ai cutover.

Storage: `public_request_log` collection (one doc per call). Cap is
soft (last 5000 rows visible at any time) — operator can hit
DELETE /api/admin/public-traffic to clear.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from auth import get_current_user
from db import db
from namespaces import PUBLIC_REQUEST_LOG


PUBLIC_PATH_PREFIX = "/api/public/"


async def public_traffic_middleware(request: Request, call_next):
    """Mounted globally; only logs paths under /api/public/*.

    Captured fields:
      path, method, query, status_code, latency_ms, tier_header,
      caller_ip (best-effort from X-Forwarded-For), ts.
    """
    is_public = request.url.path.startswith(PUBLIC_PATH_PREFIX)
    started = time.perf_counter()
    response = await call_next(request)
    if not is_public:
        return response

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    tier = request.headers.get("X-RiseDual-User-Tier", "") or "(unset)"
    caller_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )

    log_row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "path": request.url.path,
        "method": request.method,
        "query": str(request.url.query) if request.url.query else "",
        "status": response.status_code,
        "latency_ms": latency_ms,
        "tier": tier,
        "caller_ip": caller_ip,
    }
    # Fire-and-forget — never break the live request because logging
    # had a hiccup. Schedule the insert without awaiting it.
    import asyncio
    asyncio.create_task(_safe_log(log_row))
    return response


async def _safe_log(row: dict) -> None:
    try:
        await db[PUBLIC_REQUEST_LOG].insert_one(row)
    except Exception:  # noqa: BLE001
        # Logging is opportunistic. Swallow — never propagate.
        pass


# ──────────────────────── operator-read endpoints ────────────────────────

router = APIRouter(tags=["admin"])


@router.get("/admin/public-traffic")
async def list_public_traffic(
    limit: int = Query(default=200, ge=1, le=2000),
    path_contains: Optional[str] = Query(default=None),
    status: Optional[int] = Query(default=None),
    tier: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),
):
    """Last N rows from the request log, newest first. Operator-only."""
    q: dict = {}
    if path_contains:
        q["path"] = {"$regex": path_contains, "$options": "i"}
    if status is not None:
        q["status"] = status
    if tier:
        q["tier"] = tier
    rows = await db[PUBLIC_REQUEST_LOG].find(q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(limit)
    return {"items": rows, "count": len(rows)}


@router.get("/admin/public-traffic/summary")
async def public_traffic_summary(
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),
):
    """Aggregate counts over the last `hours` window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = await db[PUBLIC_REQUEST_LOG].find(
        {"ts": {"$gte": cutoff}},
        {"_id": 0, "path": 1, "status": 1, "tier": 1, "latency_ms": 1},
    ).to_list(20000)

    if not rows:
        return {
            "hours": hours, "total": 0,
            "by_endpoint": [], "by_tier": [], "by_status": [],
            "latency_p50_ms": None, "latency_p95_ms": None,
            "latency_p99_ms": None,
        }

    by_endpoint: Counter = Counter(r["path"] for r in rows)
    by_tier: Counter = Counter(r.get("tier") or "(unset)" for r in rows)
    by_status: Counter = Counter(r["status"] for r in rows)

    latencies = sorted(float(r.get("latency_ms") or 0) for r in rows)
    n = len(latencies)
    def _pct(p: float) -> float:
        idx = max(0, min(n - 1, int(n * p)))
        return round(latencies[idx], 2)

    return {
        "hours": hours,
        "total": n,
        "by_endpoint": [
            {"endpoint": ep, "count": c}
            for ep, c in by_endpoint.most_common()
        ],
        "by_tier": [{"tier": t, "count": c} for t, c in by_tier.most_common()],
        "by_status": [
            {"status": s, "count": c} for s, c in sorted(by_status.items())
        ],
        "latency_p50_ms": _pct(0.50),
        "latency_p95_ms": _pct(0.95),
        "latency_p99_ms": _pct(0.99),
    }


@router.delete("/admin/public-traffic")
async def clear_public_traffic(_user: dict = Depends(get_current_user)):
    r = await db[PUBLIC_REQUEST_LOG].delete_many({})
    return {"deleted": r.deleted_count}
