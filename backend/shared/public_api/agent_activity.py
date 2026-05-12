"""Public /agent-activity — the live narrative feed.

Polled endpoint matching risedual.ai's
`services/agent_activity_service.log_event(...)` shape:

    {
      "event_id":  "uuid4",
      "timestamp": "2026-02-13T10:11:12+00:00",  # ISO 8601 UTC
      "type":      "paper_trade_open",            # see ALLOWED_TYPES
      "severity":  "info",                        # info|success|warn|error
      "title":     "Opened INTC SHORT · $1,000",
      "detail":    "Confidence 72%, regime risk_off",
      "symbol":    "INTC",
      "metadata":  {...}
    }

The feed is synthesized from MC's existing event streams:
  * Position state changes  (from shared_position_audit)
  * Stance posts            (from shared_position_audit)
  * Conflicts detected      (from shared_brain_conflicts)
  * Outcomes resolved       (from shared_brain_outcomes)

Polling: risedual.ai's frontend hits this every ~10s with
`?since=ISO_TS` to fetch the tail.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from db import db
from namespaces import (
    SHARED_CONFLICTS,
    SHARED_OUTCOMES,
    SHARED_POSITION_AUDIT,
)

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


def _event_from_audit(row: dict) -> Optional[dict]:
    """Convert one shared_position_audit row to a feed event."""
    action = row.get("action") or "info"
    payload = row.get("payload") or {}
    actor = row.get("actor") or "system"
    pid = row.get("position_id")
    ts = row.get("ts")

    if action == "propose":
        symbol = payload.get("symbol", "")
        return {
            "event_id": f"audit-{pid}-{ts}",
            "timestamp": ts,
            "type": "signal_proposed",
            "severity": "info",
            "title": f"[propose] {symbol} signal opened by {actor}",
            "detail": f"call_mode={payload.get('call_mode', 'manual')}",
            "symbol": symbol,
            "metadata": {"position_id": pid, "actor": actor},
        }
    if action == "stance":
        sev = "info"
        stance = payload.get("stance", "?")
        if stance == "long":
            sev = "success"
        elif stance == "short":
            sev = "warn"
        return {
            "event_id": f"audit-{pid}-{ts}",
            "timestamp": ts,
            "type": "stance_posted",
            "severity": sev,
            "title": f"[{payload.get('posted_as') or 'seat'}] {actor} → {stance.upper()}",
            "detail": (
                f"confidence={payload.get('confidence')} · "
                f"may_execute={payload.get('may_execute')}"
            ),
            "symbol": None,
            "metadata": {"position_id": pid, **payload},
        }
    if action in ("executor_call", "executor_call_auto"):
        direction = payload.get("direction", "?")
        sev = "success" if direction == "long" else "warn"
        return {
            "event_id": f"audit-{pid}-{ts}",
            "timestamp": ts,
            "type": "paper_trade_open",
            "severity": sev,
            "title": f"Opened {direction.upper()} on signal {pid[:8]}",
            "detail": (
                f"executor={payload.get('executor')} · "
                f"trigger={payload.get('trigger') or action}"
            ),
            "symbol": None,
            "metadata": {"position_id": pid, **payload},
        }
    if action == "reject":
        return {
            "event_id": f"audit-{pid}-{ts}",
            "timestamp": ts,
            "type": "paper_trade_skip",
            "severity": "info",
            "title": f"Skipped signal {pid[:8]}",
            "detail": (payload.get("notes") or "")[:200],
            "symbol": None,
            "metadata": {"position_id": pid, **payload},
        }
    return None


def _event_from_conflict(row: dict) -> dict:
    return {
        "event_id": f"conflict-{row.get('conflict_id') or row.get('pair_id')}",
        "timestamp": row.get("detected_at") or row.get("created_at"),
        "type": "info",
        "severity": "warn",
        "title": f"Conflict detected · {row.get('a_brain')} vs {row.get('b_brain')}",
        "detail": (row.get("topic") or "")[:200],
        "symbol": None,
        "metadata": {"conflict_id": row.get("conflict_id")},
    }


def _event_from_outcome(row: dict) -> dict:
    won = bool(row.get("won"))
    return {
        "event_id": f"outcome-{row.get('opinion_id') or row.get('outcome_id')}",
        "timestamp": row.get("resolved_at"),
        "type": "prediction_resolved",
        "severity": "success" if won else "warn",
        "title": f"Outcome resolved · {'WIN' if won else 'LOSS'}",
        "detail": (row.get("notes") or "")[:200],
        "symbol": row.get("symbol"),
        "metadata": {k: row.get(k) for k in ("topic", "regime", "brain")},
    }


@router.get("/public/agent-activity/feed")
async def get_agent_activity(
    since: Optional[str] = Query(default=None,
                                 description="ISO 8601; return events strictly after this ts"),
    limit: int = Query(default=50, ge=1, le=200),
    caller: PublicCaller = Depends(public_trust_required),
):
    """Polled feed. risedual.ai's frontend polls every ~10s."""
    ts_filter: dict = {}
    if since:
        ts_filter = {"$gt": since}

    audit_q: dict = {}
    if ts_filter:
        audit_q["ts"] = ts_filter
    audit_rows = await db[SHARED_POSITION_AUDIT].find(audit_q, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(limit * 2)

    conflict_q: dict = {}
    if ts_filter:
        conflict_q["detected_at"] = ts_filter
    conflicts = await db[SHARED_CONFLICTS].find(conflict_q, {"_id": 0}).sort(
        "detected_at", -1,
    ).to_list(limit)

    outcome_q: dict = {}
    if ts_filter:
        outcome_q["resolved_at"] = ts_filter
    outcomes = await db[SHARED_OUTCOMES].find(outcome_q, {"_id": 0}).sort(
        "resolved_at", -1,
    ).to_list(limit)

    events: list[dict] = []
    for r in audit_rows:
        ev = _event_from_audit(r)
        if ev:
            events.append(ev)
    for r in conflicts:
        events.append(_event_from_conflict(r))
    for r in outcomes:
        events.append(_event_from_outcome(r))

    # Sort desc by timestamp; truncate.
    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    events = events[:limit]

    return {
        "items": events,
        "count": len(events),
        "since": since,
        "polled_at": _now_iso(),
        "tier": caller.tier,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
