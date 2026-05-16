"""Unified Decisions Feed.

Doctrine context (2026-02-15): every brain emits some kind of decision
trace, but those traces land in different collections:

  Alpha / Camaro      → `shared_adl_receipts` + `shared_intents`
  Chevelle (governor) → `shared_adl_receipts` (authority_call action)
  REDEYE (opponent)   → `sovereign_audit_log` (contribution action) +
                        `mc_shelly` (training signals)

Different stores, same operator question: *"what did this brain
decide?"*. This module unifies all of them behind one endpoint so the
Diagnostics page (and any future consumer) can show every brain's
output in a single feed, regardless of which collection the engine
wrote to.

Endpoint: GET /api/admin/decisions

Normalized row shape:
    {
      "ts": ISO timestamp,
      "brain": runtime identity (alpha|camaro|chevelle|redeye),
      "source_collection": which collection this row came from,
      "kind": receipt | sovereign_audit | intent | training_signal,
      "action": brain-emitted action label (best-effort extraction),
      "symbol": best-effort symbol extraction,
      "summary": one-line human-readable summary,
      "raw": the original document (for forensics / payload inspection)
    }
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import (
    MC_SHELLY,
    SHARED_INTENTS,
    SHARED_RECEIPTS,
    SOVEREIGN_AUDIT_LOG,
)

router = APIRouter(tags=["decisions"])

# Brain identity may live under any of these fields depending on the
# emitting engine's convention.
_BRAIN_FIELDS = ("runtime", "brain", "stack", "source", "from")


def _brain_clause(brain: str) -> dict:
    """Schema-tolerant brain match — accepts any of the conventional
    identity field names and any case variant."""
    variants = list({brain, brain.lower(), brain.upper(), brain.capitalize()})
    return {"$or": [{f: {"$in": variants}} for f in _BRAIN_FIELDS]}


def _extract(doc: dict, *paths: str):
    """Try a list of dotted paths and return the first non-None value."""
    for path in paths:
        cur = doc
        ok = True
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur is not None:
            return cur
    return None


def _normalize_receipt(doc: dict) -> dict:
    """Receipts come from `shared_adl_receipts`. May be intent envelopes,
    authority_calls, audit appends, or order receipts."""
    action = doc.get("action") or doc.get("kind") or "receipt"
    symbol = _extract(doc, "intent.symbol", "symbol", "payload.symbol", "data.symbol")
    confidence = _extract(doc, "intent.confidence", "intent.calibrated_confidence", "confidence")
    executable = _extract(doc, "intent.executable", "executable")
    reason = _extract(doc, "intent.execution_gate_reason", "reason")

    bits = [action]
    if symbol:
        bits.append(symbol)
    if confidence is not None:
        try:
            bits.append(f"conf={float(confidence):.2f}")
        except (TypeError, ValueError):
            pass
    if executable is False:
        bits.append("executable=false")
        if reason:
            bits.append(reason)
    elif executable is True:
        bits.append("executable=true")
    summary = " · ".join(bits)

    return {
        "ts": doc.get("timestamp") or doc.get("ts") or doc.get("created_at"),
        "brain": doc.get("runtime") or doc.get("brain") or doc.get("stack"),
        "source_collection": SHARED_RECEIPTS,
        "kind": "receipt",
        "action": action,
        "symbol": symbol,
        "summary": summary,
        "raw": doc,
    }


def _normalize_sovereign(doc: dict) -> dict:
    """REDEYE's primary store. `action: contribution` is the typical row."""
    action = doc.get("action") or doc.get("kind") or "sovereign_event"
    payload = doc.get("payload") or doc.get("data") or doc.get("contribution") or {}
    if not isinstance(payload, dict):
        payload = {}
    symbol = payload.get("symbol") or doc.get("symbol")
    side = payload.get("side") or payload.get("stance") or payload.get("bias") or doc.get("side")
    conf = payload.get("confidence") or payload.get("conviction") or doc.get("confidence")
    posted_as = doc.get("posted_as")

    bits = [action]
    if posted_as:
        bits.append(f"as {posted_as}")
    if symbol:
        bits.append(symbol)
    if side:
        bits.append(str(side))
    if conf is not None:
        try:
            bits.append(f"conf={float(conf):.2f}")
        except (TypeError, ValueError):
            pass
    # When the payload is empty (the REDEYE skeleton problem), surface
    # the structural fact rather than hiding it.
    if not payload and posted_as:
        bits.append("(empty payload — no symbol/side/conf emitted)")
    summary = " · ".join(bits)

    return {
        "ts": doc.get("ts") or doc.get("timestamp") or doc.get("created_at"),
        "brain": doc.get("brain") or doc.get("runtime") or doc.get("stack"),
        "source_collection": SOVEREIGN_AUDIT_LOG,
        "kind": "sovereign_audit",
        "action": action,
        "symbol": symbol,
        "summary": summary,
        "raw": doc,
    }


def _normalize_intent(doc: dict) -> dict:
    action = doc.get("action") or "intent"
    sym = doc.get("symbol")
    conf = doc.get("confidence")
    lane = doc.get("lane")
    bits = [action]
    if sym:
        bits.append(sym)
    if lane:
        bits.append(f"lane={lane}")
    if conf is not None:
        try:
            bits.append(f"conf={float(conf):.2f}")
        except (TypeError, ValueError):
            pass
    if doc.get("inferred_lane"):
        bits.append("(lane inferred)")
    return {
        "ts": doc.get("ingest_ts") or doc.get("ts"),
        "brain": doc.get("stack") or doc.get("runtime"),
        "source_collection": SHARED_INTENTS,
        "kind": "intent",
        "action": action,
        "symbol": sym,
        "summary": " · ".join(bits),
        "raw": doc,
    }


def _normalize_mc_shelly(doc: dict) -> dict:
    et = doc.get("event_type") or "training_signal"
    return {
        "ts": doc.get("ts") or doc.get("timestamp"),
        "brain": doc.get("brain") or doc.get("runtime"),
        "source_collection": MC_SHELLY,
        "kind": "training_signal",
        "action": et,
        "symbol": doc.get("symbol"),
        "summary": " · ".join(filter(None, [
            et,
            doc.get("symbol"),
            doc.get("action"),
            doc.get("outcome"),
            (doc.get("rationale") or "")[:80] or None,
        ])),
        "raw": doc,
    }


_SOURCES = {
    SHARED_RECEIPTS: ("timestamp", _normalize_receipt),
    SOVEREIGN_AUDIT_LOG: ("ts", _normalize_sovereign),
    SHARED_INTENTS: ("ingest_ts", _normalize_intent),
    MC_SHELLY: ("ts", _normalize_mc_shelly),
}


@router.get("/admin/decisions")
async def decisions_feed(
    brain: Optional[str] = Query(default=None, description="filter by brain identity"),
    kinds: Optional[str] = Query(
        default=None,
        description="comma-separated subset: receipt,sovereign_audit,intent,training_signal",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Unified decisions feed across all output collections.

    Lets the operator answer "what did this brain decide recently?"
    regardless of which collection the engine writes to. Each row
    carries `source_collection` so the operator can see whether a
    given decision came from a receipt, a sovereign-audit, an intent,
    or an MC training row.
    """
    wanted_kinds = (
        {k.strip() for k in kinds.split(",") if k.strip()}
        if kinds else set()
    )

    # How many rows to pull from each source. Over-fetch then merge-sort
    # so the requested `limit` reflects the truly-most-recent across
    # collections, not an artificial per-source slice.
    per_source = min(limit * 2, 200)
    rows: list[dict] = []
    counts: dict[str, int] = {}

    for coll, (sort_field, normalize) in _SOURCES.items():
        # Skip sources the caller didn't ask for.
        kind_for_coll = {
            SHARED_RECEIPTS: "receipt",
            SOVEREIGN_AUDIT_LOG: "sovereign_audit",
            SHARED_INTENTS: "intent",
            MC_SHELLY: "training_signal",
        }[coll]
        if wanted_kinds and kind_for_coll not in wanted_kinds:
            continue

        query: dict = {}
        if brain:
            query = _brain_clause(brain)
        cursor = db[coll].find(query, {"_id": 0}).sort(sort_field, -1).limit(per_source)
        coll_rows = await cursor.to_list(length=per_source)
        counts[coll] = len(coll_rows)
        rows.extend(normalize(d) for d in coll_rows)

    # Global sort by ts desc; rows without ts sink to the bottom.
    rows.sort(key=lambda r: (r.get("ts") or ""), reverse=True)

    return {
        "filter": {"brain": brain, "kinds": list(wanted_kinds) or "all", "limit": limit},
        "counts_per_source": counts,
        "items": rows[:limit],
    }
