"""Trader admin routes — operator visibility for the sidecar trader.

Doctrine pin (2026-07-01, Path 3):
    Reads come from the LOCAL trader store (SQLite + in-memory
    caches), not Mongo. This is what keeps the dashboard alive when
    Atlas is degraded.

    Mongo still receives every row via the best-effort mirror
    worker — but the dashboard doesn't wait for it.

Endpoints:
    GET  /api/admin/trader/status         — task alive? last cycle? env?
    GET  /api/admin/trader/health         — store counts + mirror lag
    GET  /api/admin/trader/receipts       — last N receipts (local SQLite)
    GET  /api/admin/trader/executions     — last N executions (local SQLite)
    POST /api/admin/trader/reload-caches  — pokes the state refresher
    POST /api/admin/trader/seed-seats     — writes the operator's canonical
                                            angel-name + brain pairings into
                                            seat_registry (Mongo). Safe to
                                            call multiple times.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
import asyncio

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.admin.trader")
router = APIRouter(prefix="/admin/trader", tags=["admin", "trader"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _import_trader():
    """Lazy import — the trader package lives at /app/trader. Cheap
    once cached by the interpreter."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from trader import state, store   # noqa: WPS433
    return state, store


@router.get("/status")
async def trader_status(_: dict = Depends(get_current_user)) -> dict:
    """Operator visibility: is the trader alive and ticking?
    Reads exclusively from local SQLite + in-memory state — this
    endpoint MUST keep serving even when Atlas is unreachable."""
    enabled = os.environ.get("TRADER_ENABLED", "false").lower() == "true"
    broker_disabled = os.environ.get("BROKER_DISABLED", "false").lower() == "true"
    auto_router_off = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "false"

    state, store = _import_trader()

    # Last receipt = proxy for "loop is ticking".
    recent = store.recent_receipts(limit=1)
    last_receipt = recent[0] if recent else None

    # Receipt count in the last 5 minutes — sanity check the loop
    # is firing on schedule. Done via a lightweight SQLite COUNT.
    five_min_iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 300, tz=timezone.utc,
    ).isoformat()
    all_recent = store.recent_receipts(limit=500)
    recent_count = sum(1 for r in all_recent if (r.get("ts") or "") >= five_min_iso)

    last_exec_rows = store.recent_executions(limit=1)
    last_execution = last_exec_rows[0] if last_exec_rows else None

    today = _today_prefix()
    fires_today_rows = store.recent_executions(limit=500, ok=True)
    fires_today = sum(1 for r in fires_today_rows
                      if (r.get("ts") or "").startswith(today))
    spent_today = sum(
        float(r.get("notional_usd") or 0.0)
        for r in fires_today_rows
        if (r.get("ts") or "").startswith(today)
    )

    return {
        "ok": True,
        "env": {
            "TRADER_ENABLED": enabled,
            "BROKER_DISABLED": broker_disabled,
            "AUTO_ROUTER_DISABLED": auto_router_off,
            "interval_sec": int(os.environ.get("TRADER_INTERVAL_SEC", "60")),
            "per_order_cap_usd": float(os.environ.get("TRADER_PER_ORDER_USD_CAP", "10")),
            "daily_cap_usd": float(os.environ.get("TRADER_DAILY_USD_CAP", "1000")),
            "crypto_pair": os.environ.get("TRADER_CRYPTO_PAIR", "XBTUSD"),
            "equity_ticker": os.environ.get("TRADER_EQUITY_TICKER", "TSLA"),
            "confidence_threshold": float(
                os.environ.get("TRADER_CONFIDENCE_THRESHOLD", "0.55")
            ),
        },
        "loop": {
            "last_receipt_ts": (last_receipt or {}).get("ts"),
            "last_receipt_lane": (last_receipt or {}).get("lane"),
            "last_receipt_symbol": (last_receipt or {}).get("symbol"),
            "receipts_last_5_min": recent_count,
            "alive_inference": recent_count > 0,
        },
        "trades": {
            "fires_today": fires_today,
            "spent_today_usd": spent_today,
            "last_execution_ts": (last_execution or {}).get("ts"),
            "last_execution_lane": (last_execution or {}).get("lane"),
            "last_execution_action": (last_execution or {}).get("action"),
            "last_execution_broker": (last_execution or {}).get("broker"),
            "last_execution_ok": (last_execution or {}).get("ok"),
        },
        "state": state.snapshot(),
        "checked_at": _now_iso(),
    }


@router.get("/health")
async def trader_health(_: dict = Depends(get_current_user)) -> dict:
    """Local store health: row counts + Mongo mirror lag. Reads
    only from SQLite; does not touch Mongo."""
    state, store = _import_trader()
    return {
        "ok": True,
        "store": store.counts(),
        "state": state.snapshot(),
        "checked_at": _now_iso(),
    }


@router.get("/receipts")
async def trader_receipts(
    _: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
    lane: Optional[str] = Query(default=None),
    fired_only: bool = Query(default=False),
) -> dict:
    """Most recent per-cycle receipts, from local SQLite. Answers:
        what did the trader see this minute?
        what did each brain say?
        what did the seat doctrine pick?
        did risk block? did broker accept?
    """
    _, store = _import_trader()
    rows = store.recent_receipts(limit=limit, lane=lane, fired_only=fired_only)
    return {"ok": True, "count": len(rows), "items": rows}


@router.get("/executions")
async def trader_executions(
    _: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
    lane: Optional[str] = Query(default=None),
    ok: Optional[bool] = Query(default=None),
) -> dict:
    """Executions written by the trader, from local SQLite. Each row
    carries the broker_response or exception_msg — 'what did
    Kraken/Webull actually say?' tape."""
    _, store = _import_trader()
    rows = store.recent_executions(limit=limit, lane=lane, ok=ok)
    return {"ok": True, "count": len(rows), "items": rows}


@router.post("/reload-caches")
async def trader_reload_caches(actor: dict = Depends(get_current_user)) -> dict:
    """Force an out-of-band pull from Mongo → in-memory cache. Used
    after the operator changes a seat assignment or flips the master
    switch and doesn't want to wait for the 60s refresh interval."""
    state, _ = _import_trader()
    poked = state.request_manual_refresh()
    return {
        "ok": True,
        "manual_refresh_queued": poked,
        "note": (
            "Refresh worker will run within a second; results visible "
            "at GET /api/admin/trader/status → state.last_refresh_ok_ts."
        ) if poked else (
            "Refresh worker is not running (trader loop not started). "
            "Set TRADER_ENABLED=true to activate the background refresher."
        ),
        "reloaded_at": _now_iso(),
        "requested_by": actor.get("email"),
    }


@router.get("/broker-check")
async def trader_broker_check(_: dict = Depends(get_current_user)) -> dict:
    """Live connectivity probe of both brokers. Read-only calls;
    never creates orders. Resolves credentials from env first,
    then Mongo (matches the trader's actual runtime lookup).

    Response shape:
        {"kraken": {connected, cred_source, error?, probe?},
         "webull": {connected, cred_source, error?, probe?}}

    Safe to run on demand from the operator dashboard.
    """
    from routes.trader_broker_check import probe_kraken, probe_webull
    kraken = await probe_kraken(db)
    webull = await probe_webull()
    return {
        "ok": True,
        "checked_at": _now_iso(),
        "kraken": kraken,
        "webull": webull,
    }


@router.post("/prune")
async def trader_prune(
    actor: dict = Depends(get_current_user),
    days: int = Query(default=7, ge=1, le=365,
                      description="Retention window in days"),
    keep_pending: bool = Query(default=True,
                               description="Refuse to prune rows not yet mirrored to Mongo"),
) -> dict:
    """Retention trim. Keeps the last `days` days of local truth in
    SQLite; anything older is dropped and space is reclaimed via
    VACUUM. Mongo mirror (best-effort archive) is the long-tail
    store.

    By default (`keep_pending=true`) rows that have not yet been
    mirrored to Mongo are preserved — so an Atlas outage does NOT
    cause silent data loss when the operator hits prune.

    Safe to schedule nightly via cron / a scheduled fetch."""
    _, store = _import_trader()
    result = await asyncio.to_thread(store.prune, days, keep_pending=keep_pending)
    return {
        "ok": True,
        "days": days,
        "pruned_at": _now_iso(),
        "pruned_by": actor.get("email"),
        **result,
    }


@router.get("/spread")
async def trader_spread(
    _: dict = Depends(get_current_user),
    symbol: Optional[str] = Query(default=None,
                                  description="Kraken pair or equity ticker"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict:
    """Bid/ask spread telemetry from the trader's spread poller.
    Returns latest per-symbol snapshot + rolling history. Reads
    entirely from local SQLite + in-memory cache — no Mongo, works
    during Atlas outages.
    """
    _, store_mod = _import_trader()
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from trader import spread as spread_mod   # noqa: WPS433

    if symbol:
        # Single-symbol view: cache row + its history
        cache_row = spread_mod.latest(symbol)
        history = store_mod.recent_spread_ticks(pair=symbol, limit=limit)
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "latest": cache_row or None,
            "stale": spread_mod.is_stale(symbol),
            "history": history,
            "checked_at": _now_iso(),
        }

    # Multi-symbol view: one snapshot per known symbol + a shared
    # rolling history feed
    latest_all = spread_mod.latest()
    # attach staleness per row
    if isinstance(latest_all, list):
        for row in latest_all:
            row["stale"] = spread_mod.is_stale(row.get("pair", ""))
    # Include the MQTT stream health so the UI can distinguish
    # `tick-by-tick stream` vs `20s HTTP snapshot` at a glance.
    try:
        from trader import spread_stream as _spread_stream_mod  # noqa: WPS433
        stream_status = _spread_stream_mod.get_status()
    except Exception:  # noqa: BLE001
        stream_status = {"state": "unavailable"}
    return {
        "ok": True,
        "latest": latest_all,
        "history": store_mod.recent_spread_ticks(limit=limit),
        "stream": stream_status,
        "config": {
            "crypto": {
                "enabled": os.environ.get(
                    "TRADER_SPREAD_ENABLED", "true"
                ).lower() == "true",
                "pairs": os.environ.get("TRADER_SPREAD_PAIRS", ""),
                "poll_sec": int(os.environ.get("TRADER_SPREAD_POLL_SEC", "15")),
                "max_bps": float(os.environ.get("TRADER_SPREAD_MAX_BPS", "50")),
                "gate_enabled": os.environ.get(
                    "TRADER_SPREAD_GATE_ENABLED", "false"
                ).lower() == "true",
            },
            "equity": {
                # 2026-07-02 default ON — switched from retired public
                # gateway to authenticated Webull OpenAPI snapshot.
                "enabled": os.environ.get(
                    "TRADER_EQUITY_SPREAD_ENABLED", "true"
                ).lower() == "true",
                "tickers": os.environ.get("TRADER_EQUITY_SPREAD_TICKERS", ""),
                "poll_sec": int(os.environ.get("TRADER_EQUITY_SPREAD_POLL_SEC", "20")),
                "max_bps": float(os.environ.get("TRADER_EQUITY_SPREAD_MAX_BPS", "25")),
                "gate_enabled": os.environ.get(
                    "TRADER_EQUITY_SPREAD_GATE_ENABLED", "false"
                ).lower() == "true",
            },
            "stale_sec": int(os.environ.get("TRADER_SPREAD_STALE_SEC", "120")),
        },
        "checked_at": _now_iso(),
    }


@router.post("/webull-token-create")
async def webull_token_create(
    _: dict = Depends(get_current_user),
) -> dict:
    """Trigger Webull's 2FA-based access-token creation flow.

    Calls `POST /openapi/auth/token/create` on the Webull OpenAPI.
    The server sends a push notification to the operator's Webull
    mobile app; approval flips the token status from PENDING to
    NORMAL. We persist the returned token to `webull_token.json`;
    `spread.py` starts using it on the next poll cycle.

    Response never contains the raw token — only a preview
    (`bf12ab…7c9d`) + length so ops can verify it was written.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from trader import webull_auth as _wa   # noqa: WPS433
    try:
        payload = await _wa.create_token()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "ok": True,
        "message": (
            "Token created. Approve the push notification in your "
            "Webull mobile app to activate it. Once status flips to "
            "NORMAL server-side, equity spreads will start flowing."
        ),
        **payload,
        "checked_at": _now_iso(),
    }


@router.get("/webull-token-status")
async def webull_token_status(
    _: dict = Depends(get_current_user),
) -> dict:
    """Cheap dashboard read — never hits Webull. Reports whether a
    persisted token exists, its status/expiry, and a preview."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from trader import webull_auth as _wa   # noqa: WPS433
    return {"ok": True, **_wa.status(), "checked_at": _now_iso()}


@router.get("/dissent")
async def trader_dissent(
    _: dict = Depends(get_current_user),
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    lane: Optional[str] = Query(default=None),
) -> dict:
    """Per-brain dissent tracker (2026-07-02).

    Preserves per-brain personality: shows how often each brain
    disagrees with the executor's chosen verdict. Higher dissent
    isn't good or bad on its own — it's a signal for where a
    brain's weights diverge from the current executor doctrine.

    Reads entirely from local SQLite; no Mongo dependency.
    """
    from datetime import datetime, timezone, timedelta
    _, store_mod = _import_trader()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).isoformat()

    # Pull enough rows to cover the window. `recent_receipts` sorts
    # by ts DESC so we can early-stop once we're past the cutoff.
    receipts = store_mod.recent_receipts(limit=10_000, lane=lane)
    # Filter to the window.
    receipts = [r for r in receipts if r.get("ts") and r["ts"] >= cutoff]

    # Aggregation shape:
    #   brains[brain] = {
    #     "cycles": int,           # times this brain produced a signal
    #     "dissents": int,         # verdict != executor.verdict
    #     "vs": { other_brain: count_of_dissents_when_they_were_executor }
    #   }
    from collections import defaultdict
    brains_agg: dict = defaultdict(
        lambda: {"cycles": 0, "dissents": 0, "vs": defaultdict(int)}
    )
    total_cycles = 0
    for r in receipts:
        chosen = r.get("chosen") or {}
        executor_brain = chosen.get("brain") or (
            (r.get("seats") or {}).get("executor")
        )
        executor_verdict = chosen.get("verdict")
        if not executor_brain or not executor_verdict:
            continue
        total_cycles += 1
        for sig in r.get("signals") or []:
            brain = sig.get("brain")
            verdict = sig.get("verdict")
            if not brain or not verdict:
                continue
            brains_agg[brain]["cycles"] += 1
            if brain != executor_brain and verdict != executor_verdict:
                brains_agg[brain]["dissents"] += 1
                brains_agg[brain]["vs"][executor_brain] += 1
    out = []
    for brain, agg in brains_agg.items():
        cycles = agg["cycles"] or 1
        out.append({
            "brain": brain,
            "cycles": agg["cycles"],
            "dissents": agg["dissents"],
            "dissent_rate_pct": round(agg["dissents"] / cycles * 100, 1),
            "top_dissents_vs": dict(
                sorted(agg["vs"].items(), key=lambda kv: -kv[1])[:5]
            ),
        })
    out.sort(key=lambda r: -r["dissent_rate_pct"])
    return {
        "ok": True,
        "window_hours": window_hours,
        "total_cycles": total_cycles,
        "brains": out,
        "checked_at": _now_iso(),
    }


@router.get("/brain-accuracy")
async def trader_brain_accuracy(
    _: dict = Depends(get_current_user),
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    lane: Optional[str] = Query(default=None),
) -> dict:
    """Per-brain execution outcome tracker (2026-07-03).

    Joins receipts ↔ executions via `intent_id` and reports, per
    brain that ACTED as executor:
      * fires     — times this brain drove the executor seat
      * fills     — executions where the broker accepted
      * fill_rate_pct
      * avg_confidence — signal confidence at fire time
      * confidence_p10 / p50 / p90 — DISTRIBUTION (bimodal detector)
      * avg_spread_bps_at_fire, avg_quote_age_ms_at_fire
      * quote_age_ms_p50 — median freshness (CFQS input)
      * avg_notional_usd
      * broker_error_rate_pct
      * cfqs — Calibrated Fill Quality Score + merge-rights gates
               (see /app/trader/merge_rights.py for the locked formula)

    Position-outcome (win/loss/PnL) tracking is deferred until the
    trader has round-trip position lifecycle; this endpoint
    intentionally stops at the fill boundary so the numbers we
    show are numbers we can defend.
    """
    from datetime import datetime, timezone, timedelta
    _, store_mod = _import_trader()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).isoformat()

    receipts = store_mod.recent_receipts(limit=10_000, lane=lane)
    receipts = [r for r in receipts if r.get("ts") and r["ts"] >= cutoff]

    executions = store_mod.recent_executions(limit=10_000, lane=lane)
    exec_by_intent = {e["intent_id"]: e for e in executions}

    from collections import defaultdict
    agg: dict = defaultdict(lambda: {
        "fires": 0, "fills": 0,
        "confidences": [],  # keep the full list so we can report percentiles
        "spread_sum": 0.0, "spread_n": 0,
        "age_sum": 0.0, "age_n": 0,
        "notional_sum": 0.0, "notional_n": 0,
        "broker_errors": 0,
    })
    for r in receipts:
        chosen = r.get("chosen") or {}
        brain = chosen.get("brain")
        if not brain:
            continue
        intent_id = f"trader-{(r.get('cycle_id') or '')[:16]}-{r.get('lane')}"
        exec_row = exec_by_intent.get(intent_id)
        if not exec_row:
            continue
        a = agg[brain]
        a["fires"] += 1
        if exec_row.get("ok"):
            a["fills"] += 1
        if exec_row.get("exception_type"):
            a["broker_errors"] += 1
        conf = chosen.get("confidence")
        if isinstance(conf, (int, float)):
            a["confidences"].append(float(conf))
        q = r.get("quote") or {}
        sp = q.get("spread_bps")
        if isinstance(sp, (int, float)):
            a["spread_sum"] += float(sp)
            a["spread_n"] += 1
        ag = q.get("quote_age_ms")
        if isinstance(ag, (int, float)):
            a["age_sum"] += float(ag)
            a["age_n"] += 1
        n = exec_row.get("notional_usd")
        if isinstance(n, (int, float)):
            a["notional_sum"] += float(n)
            a["notional_n"] += 1

    def _avg(num: float, den: int) -> Optional[float]:
        return round(num / den, 4) if den else None

    def _pct(vals: list, q: float) -> Optional[float]:
        """Percentile via statistics.quantiles; returns None if the
        sample can't produce a meaningful boundary."""
        if not vals:
            return None
        import statistics
        if len(vals) < 2:
            return round(vals[0], 4)
        try:
            qs = statistics.quantiles(sorted(vals), n=100)
            idx = min(max(int(q * 100) - 1, 0), 98)
            return round(qs[idx], 4)
        except statistics.StatisticsError:
            return round(sum(vals) / len(vals), 4)

    # Also need the p50 quote age per brain for the CFQS freshness
    # factor. Keep the raw list so we can percentile it below.
    age_lists: dict = {}
    for r in receipts:
        chosen = r.get("chosen") or {}
        brain = chosen.get("brain")
        if not brain or brain not in agg:
            continue
        intent_id = f"trader-{(r.get('cycle_id') or '')[:16]}-{r.get('lane')}"
        if intent_id not in exec_by_intent:
            continue
        q = r.get("quote") or {}
        ag = q.get("quote_age_ms")
        if isinstance(ag, (int, float)):
            age_lists.setdefault(brain, []).append(float(ag))

    # First pass: per-brain averages, so the SECOND pass can compute
    # the lane-median spread to feed CFQS's spread_penalty.
    prelim = []
    for brain, a in agg.items():
        fires = a["fires"] or 1
        conf_list = a["confidences"]
        prelim.append({
            "brain": brain,
            "fires": a["fires"],
            "fills": a["fills"],
            "broker_errors": a["broker_errors"],
            "fill_rate_pct": round(a["fills"] / fires * 100, 1),
            "avg_confidence": (
                round(sum(conf_list) / len(conf_list), 4)
                if conf_list else None
            ),
            # Confidence DISTRIBUTION — surface p10/p50/p90 so a
            # re-baseline pass can spot bimodal splits that a mean
            # would hide (e.g. one bucket confident-right, one bucket
            # confident-wrong at similar avg but very different tails).
            "confidence_p10": _pct(conf_list, 0.10),
            "confidence_p50": _pct(conf_list, 0.50),
            "confidence_p90": _pct(conf_list, 0.90),
            "confidence_n": len(conf_list),
            "avg_spread_bps_at_fire": _avg(a["spread_sum"], a["spread_n"]),
            "avg_quote_age_ms_at_fire": _avg(a["age_sum"], a["age_n"]),
            "quote_age_ms_p50": _pct(age_lists.get(brain, []), 0.50),
            "avg_notional_usd": _avg(a["notional_sum"], a["notional_n"]),
            "broker_error_rate_pct": round(
                a["broker_errors"] / fires * 100, 1
            ),
        })

    # Lane-median spread — CFQS's spread_penalty compares each brain
    # against the median of its peers on the SAME lane. Cross-lane
    # comparison is doctrinally banned (crypto ≠ equity regime).
    # When the endpoint is called with a `lane` filter, all rows are
    # already same-lane, so the median is over all of them.
    spread_samples = [
        p["avg_spread_bps_at_fire"] for p in prelim
        if isinstance(p["avg_spread_bps_at_fire"], (int, float))
    ]
    lane_median_spread_bps: Optional[float] = None
    if spread_samples:
        import statistics
        lane_median_spread_bps = round(
            statistics.median(spread_samples), 4
        )

    # Second pass: attach CFQS + merge-rights breakdown per brain.
    from trader.merge_rights import compute_cfqs

    out = []
    for p in prelim:
        cfqs = compute_cfqs(
            fires=p["fires"],
            fills=p["fills"],
            broker_errors=p["broker_errors"],
            confidence_n=p["confidence_n"],
            p50_quote_age_ms=p["quote_age_ms_p50"],
            avg_spread_bps=p["avg_spread_bps_at_fire"],
            lane_median_spread_bps=lane_median_spread_bps,
            confidence_p10=p["confidence_p10"],
            confidence_p90=p["confidence_p90"],
        )
        # `broker_errors` is a computation input, not an operator-
        # facing field — drop it before emitting.
        p.pop("broker_errors", None)
        p["cfqs"] = cfqs.to_dict()
        out.append(p)

    out.sort(key=lambda r: -r["fires"])
    return {
        "ok": True,
        "window_hours": window_hours,
        "lane_median_spread_bps": lane_median_spread_bps,
        "brains": out,
        "checked_at": _now_iso(),
    }


# Operator-canonical angel→brain pairings for the trader.
# Documented in /app/trader/state.py::DEFAULT_SEATS. Repeated here so
# the seed endpoint can write them without importing the trader
# module (decoupled from trader's lifecycle).
_OPERATOR_SEAT_PAIRINGS = [
    # Equity lane
    {"lane": "equity", "role": "strategist", "angel": "Raziel",  "holder": "camino"},
    {"lane": "equity", "role": "governor",   "angel": "Nuriel",  "holder": "hellcat",
     "risk_multiplier": 1.0},
    {"lane": "equity", "role": "executor",   "angel": "Paschar", "holder": "gto"},
    {"lane": "equity", "role": "auditor",    "angel": "Sariel",  "holder": "barracuda"},
    # Crypto lane
    {"lane": "crypto", "role": "strategist", "angel": "Remiel",  "holder": "hellcat"},
    {"lane": "crypto", "role": "governor",   "angel": "Cassiel", "holder": "camino",
     "risk_multiplier": 1.0},
    {"lane": "crypto", "role": "executor",   "angel": "Israfel", "holder": "gto"},
    {"lane": "crypto", "role": "auditor",    "angel": "Zadkiel", "holder": "barracuda"},
]


@router.post("/seed-seats")
async def trader_seed_seats(actor: dict = Depends(get_current_user)) -> dict:
    """Idempotent seat-registry seeder.

    Writes the operator-canonical angel→brain pairings into the
    `seat_registry` collection (Mongo). Safe to call repeatedly —
    uses upsert semantics with `$set` so an existing row's other
    fields (like `last_changed_at` audit) are preserved.
    """
    now = _now_iso()
    results = []
    for p in _OPERATOR_SEAT_PAIRINGS:
        sid = f"{p['lane']}:{p['role']}"
        set_fields = {
            "lane": p["lane"],
            "role": p["role"],
            "angel": p["angel"],
            "holder": p["holder"],
            "assigned_by": (actor.get("email") or "operator-seed"),
            "reason": "seeded_by_admin_trader_seed_seats",
            "last_changed_at": now,
            "since": now,
        }
        if "risk_multiplier" in p:
            set_fields["risk_multiplier"] = p["risk_multiplier"]
        await db["seat_registry"].update_one(
            {"_id": sid},
            {"$set": set_fields, "$setOnInsert": {"_id": sid}},
            upsert=True,
        )
        results.append({
            "id": sid,
            "angel": p["angel"],
            "role": p["role"],
            "lane": p["lane"],
            "holder": p["holder"],
        })
    # After seeding Mongo, poke the trader's state cache so the
    # change is picked up without waiting for the 60s refresh.
    try:
        state, _ = _import_trader()
        state.request_manual_refresh()
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "applied_at": now,
        "applied_by": actor.get("email"),
        "count": len(results),
        "seats": results,
    }
