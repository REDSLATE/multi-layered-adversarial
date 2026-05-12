"""Public /digest — daily market digest.

Output shape mirrors risedual.ai's
`services/digest_service.collect_digest_data(db)`:

    {
      "predictions": [{symbol, direction, confidence, price}],
      "smart_money": [{symbol, score, signal, net_flow_usd, bullish, bearish}],
      "alerts":      [{symbol, delta, signal_change}],
      "overview":    {regime, summary, ...},
      "watchlist":   {...},      # filled by risedual.ai (it knows the user)
      "caps":        {predictions, smart_money, alerts},
      "tier":        "free|starter|pro|pro_max"
    }

Tier rule (matches risedual.ai's behavior):
  * Non-paid tiers (free, starter):  caps = 2 predictions / 2 smart_money / 1 alert
  * Paid tiers   (pro, pro_max):      caps = unlimited (we cap at 25 for sanity)

Source data: MC's open positions + indicator snapshots. We synthesize
predictions from the latest stances + state, smart_money from stance
scores aggregated across symbols, alerts from recent governor-flagged
positions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from db import db
from namespaces import (
    SHARED_INDICATOR_SNAPSHOTS,
    SHARED_POSITIONS,
    SHARED_POSITION_STANCES,
)
from shared.positions import OPEN_STATES

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


FREE_CAPS = {"predictions": 2, "smart_money": 2, "alerts": 1}
PAID_CAPS = {"predictions": 25, "smart_money": 25, "alerts": 25}


def _direction_label(stance: str | None, state: str) -> str:
    if state == "consensus_long":
        return "LONG"
    if state == "consensus_short":
        return "SHORT"
    if stance == "long":
        return "LONG"
    if stance == "short":
        return "SHORT"
    if stance == "abstain":
        return "HOLD"
    return "NO_TRADE"


async def _build_predictions(limit: int) -> list[dict]:
    """One row per open position; direction + confidence from the
    aggregate of stances. Sorted by recency."""
    positions = await db[SHARED_POSITIONS].find(
        {"state": {"$in": list(OPEN_STATES)}}, {"_id": 0},
    ).sort("updated_at", -1).to_list(limit * 3)

    out: list[dict] = []
    for p in positions:
        stances = await db[SHARED_POSITION_STANCES].find(
            {"position_id": p["position_id"]}, {"_id": 0},
        ).sort("posted_at", -1).to_list(16)
        if not stances:
            continue

        # Latest stance per brain (refinements supersede).
        latest_by_brain: dict[str, dict] = {}
        for s in stances:
            latest_by_brain.setdefault(s["brain"], s)
        votes = list(latest_by_brain.values())
        if not votes:
            continue

        # Majority stance + averaged confidence among that stance.
        counts: dict[str, list[float]] = {"long": [], "short": [], "abstain": []}
        for v in votes:
            counts.setdefault(v["stance"], []).append(float(v.get("confidence", 0.0)))
        winner = max(counts.items(), key=lambda kv: len(kv[1]))[0]
        confs = counts[winner] or [0.5]
        confidence_pct = round(sum(confs) / len(confs) * 100)

        # Optional price from indicator snapshot if we have it.
        snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
            {"symbol": p["symbol"]}, {"_id": 0, "indicators.last_close": 1},
        )
        price = None
        if snap:
            price = (snap.get("indicators") or {}).get("last_close")

        out.append({
            "symbol": p["symbol"],
            "direction": _direction_label(winner, p["state"]),
            "confidence": confidence_pct,
            "price": price,
        })
        if len(out) >= limit:
            break
    return out


async def _build_smart_money(limit: int) -> list[dict]:
    """Per-symbol aggregated bullish/bearish stance counts. A proxy for
    'smart money flow' until we wire real options-flow / order-book
    data — uses brain conviction strength as the score."""
    pipeline = [
        {"$match": {"stance": {"$in": ["long", "short"]}}},
        {"$group": {
            "_id": {
                "position_id": "$position_id",
                "brain": "$brain",
            },
            "stance": {"$last": "$stance"},
            "confidence": {"$last": "$confidence"},
            "posted_at": {"$last": "$posted_at"},
        }},
        # rejoin to position so we can pick up the symbol
    ]
    rows = await db[SHARED_POSITION_STANCES].aggregate(pipeline).to_list(2000)

    # Translate position_id → symbol.
    pos_ids = list({r["_id"]["position_id"] for r in rows})
    if not pos_ids:
        return []
    positions = await db[SHARED_POSITIONS].find(
        {"position_id": {"$in": pos_ids}}, {"_id": 0, "position_id": 1, "symbol": 1},
    ).to_list(len(pos_ids))
    pid_to_sym = {p["position_id"]: p["symbol"] for p in positions}

    by_sym: dict[str, dict] = {}
    for r in rows:
        sym = pid_to_sym.get(r["_id"]["position_id"])
        if not sym:
            continue
        bucket = by_sym.setdefault(sym, {
            "bullish": 0, "bearish": 0, "bull_conf": [], "bear_conf": [],
        })
        if r["stance"] == "long":
            bucket["bullish"] += 1
            bucket["bull_conf"].append(float(r.get("confidence", 0.0)))
        else:
            bucket["bearish"] += 1
            bucket["bear_conf"].append(float(r.get("confidence", 0.0)))

    out: list[dict] = []
    for sym, b in by_sym.items():
        bull = b["bullish"]
        bear = b["bearish"]
        total = max(1, bull + bear)
        # Score: 50 = neutral, 100 = all-bullish-high-conf, 0 = all-bearish-high-conf.
        bull_w = sum(b["bull_conf"]) if b["bull_conf"] else 0
        bear_w = sum(b["bear_conf"]) if b["bear_conf"] else 0
        score = round(50 + ((bull_w - bear_w) / total) * 50)
        score = max(0, min(100, score))
        if score >= 60:
            signal = "bullish"
        elif score <= 40:
            signal = "bearish"
        else:
            signal = "neutral"
        out.append({
            "symbol": sym,
            "score": score,
            "signal": signal,
            "net_flow_usd": None,    # not wired yet (no order-book data)
            "bullish": bull,
            "bearish": bear,
        })

    out.sort(key=lambda r: abs(r["score"] - 50), reverse=True)
    return out[:limit]


async def _build_alerts(limit: int) -> list[dict]:
    """Auditor-flagged positions in the last 24h. delta is derived from
    the count of stance flips on the position (rough proxy for 'score
    change' until we wire historical sentiment scoring)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    pos = await db[SHARED_POSITIONS].find(
        {"updated_at": {"$gte": cutoff}}, {"_id": 0},
    ).sort("updated_at", -1).to_list(limit * 3)

    out: list[dict] = []
    for p in pos:
        stances = await db[SHARED_POSITION_STANCES].find(
            {"position_id": p["position_id"]}, {"_id": 0},
        ).sort("posted_at", 1).to_list(32)
        if len(stances) < 2:
            continue
        # Look for sign of dissent: governor going long/short while decider went opposite.
        last_by_seat: dict[str, dict] = {}
        for s in stances:
            seat = s.get("posted_as")
            if seat:
                last_by_seat[seat] = s
        governor = last_by_seat.get("governor")
        decider = last_by_seat.get("decider")
        if not (governor and decider):
            continue
        if governor["stance"] == "abstain":
            continue
        if decider["stance"] == "abstain":
            continue
        if governor["stance"] == decider["stance"]:
            continue
        out.append({
            "symbol": p["symbol"],
            "delta": -round(float(governor.get("confidence", 0.5)) * 50),
            "signal_change": f"{decider['stance']}→{governor['stance']}_flagged",
        })
        if len(out) >= limit:
            break
    return out


def _locked_row(kind: str) -> dict:
    """Free-tier stand-in for paywalled rows."""
    return {
        "symbol": None,
        "locked": True,
        "kind": kind,
        "upgrade_to": "pro",
        "cta": f"Unlock more {kind} with Pro",
    }


@router.get("/public/digest")
async def get_public_digest(
    caller: PublicCaller = Depends(public_trust_required),
):
    caps = PAID_CAPS if caller.is_paid else FREE_CAPS

    # Pull enough data to fill paid caps; we'll truncate + lock for free tier.
    full_preds = await _build_predictions(PAID_CAPS["predictions"])
    full_sm = await _build_smart_money(PAID_CAPS["smart_money"])
    full_alerts = await _build_alerts(PAID_CAPS["alerts"])

    predictions = full_preds[:caps["predictions"]]
    smart_money = full_sm[:caps["smart_money"]]
    alerts = full_alerts[:caps["alerts"]]

    # Free tier sees one locked-CTA row per category if there's MORE
    # available (matches risedual.ai's `_locked_more_row` behavior).
    if not caller.is_paid:
        if len(full_preds) > caps["predictions"]:
            predictions = predictions + [_locked_row("predictions")]
        if len(full_sm) > caps["smart_money"]:
            smart_money = smart_money + [_locked_row("smart_money")]
        if len(full_alerts) > caps["alerts"]:
            alerts = alerts + [_locked_row("alerts")]

    # Overview synthesizes the aggregate market posture from active signals.
    open_count = await db[SHARED_POSITIONS].count_documents(
        {"state": {"$in": list(OPEN_STATES)}},
    )
    return {
        "predictions": predictions,
        "smart_money": smart_money,
        "alerts": alerts,
        "overview": {
            "active_signals": open_count,
            "summary": (
                "Multi-brain consensus across active positions. Strategist "
                "proposes; Auditor vetoes; Commander synthesizes."
            ),
        },
        "watchlist": None,    # risedual.ai fills this from its own user-watchlist store
        "caps": caps,
        "tier": caller.tier,
    }
