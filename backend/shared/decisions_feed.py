"""Unified Decisions Feed.

Doctrine context (2026-02-15): every brain emits some kind of decision
trace, but those traces land in different collections:

  Alpha / Camaro      → `shared_adl_receipts` + `shared_intents`
  Chevelle (governor) → `shared_adl_receipts` (authority_call action)
  REDEYE (opponent)   → `sovereign_audit_log` (contribution action) +
                        `mc_shelly` (engine audit events — gate_pass,
                        gate_fail, intent_ingested, council_pass/block,
                        position lifecycle, sidecar packets)

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
      "kind": receipt | sovereign_audit | intent | engine_audit,
      "action": brain-emitted action label (best-effort extraction),
      "symbol": best-effort symbol extraction,
      "summary": one-line human-readable summary,
      "raw": the original document (for forensics / payload inspection)
    }

Doctrine note (2026-05-23): the `engine_audit` kind previously
surfaced as `training_signal` in the UI — that label was misleading
because mc_shelly captures REAL live engine events (passing/failing
gates, real intent ingest, real position lifecycle), not training-
only shadow signals. Renamed for accuracy. The legacy
`training_signal` filter value is still accepted as an alias.
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
    """REDEYE's primary store. `action: contribution` is the typical row.

    Doctrine pin (2026-05-24): contributions are PERIODIC GLOBAL-STATE
    snapshots, not per-symbol opinions. The renderer used to expect a
    `payload` dict carrying `symbol/side/confidence`, but the audit
    writer stores those fields at the top level (after 2026-05-23) and
    contributions intentionally don't carry a single triggering symbol
    — a 60-sec contribution may span multiple decisions in its window.

    The summary line now reads contribution data off the audit-row top
    level: `mode`, `notes`, `weights`, `recent_outcomes` count, and
    (when recent_outcomes has entries) extracts the most-recent
    symbol/action/confidence as a display anchor. This is presentation
    only — it does NOT constrain the contribution schema."""
    action = doc.get("action") or doc.get("kind") or "sovereign_event"
    # Back-compat: some older rows did store a `payload` dict. Fall
    # back to the top-level keys (the canonical home post-2026-05-23).
    payload = doc.get("payload") or doc.get("data") or doc.get("contribution") or {}
    if not isinstance(payload, dict):
        payload = {}

    posted_as = doc.get("posted_as")
    bits = [action]
    if posted_as:
        bits.append(f"as {posted_as}")

    # For `contribution` rows, the doctrinal data lives at the top
    # level of the audit row (mode/notes/weights/recent_outcomes/...).
    # Surface what's actually there instead of demanding per-symbol
    # fields contributions weren't designed to carry.
    symbol = payload.get("symbol") or doc.get("symbol")
    side = payload.get("side") or payload.get("stance") or payload.get("bias") or doc.get("side")
    conf = payload.get("confidence") or payload.get("conviction") or doc.get("confidence")

    if action == "contribution":
        # Extract a display anchor from recent_outcomes (most recent
        # entry) when present. Does NOT promote it to a schema field.
        recent = doc.get("recent_outcomes") or []
        if isinstance(recent, list) and recent and not symbol:
            first = recent[0] if isinstance(recent[0], dict) else {}
            symbol = first.get("symbol")
            side = side or first.get("action")
            conf = conf if conf is not None else first.get("confidence")

        # If the symbol/side/conf came in via the legacy payload dict
        # (very old contribution rows pre-dating the 2026-05-23 fix),
        # surface those directly — same shape as non-contribution rows.
        if symbol and not (doc.get("mode") or doc.get("weights") or doc.get("notes") or recent):
            bits.append(symbol)
            if side:
                bits.append(str(side))
            if conf is not None:
                try:
                    bits.append(f"conf={float(conf):.2f}")
                except (TypeError, ValueError):
                    pass
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

        # Substance fields the audit row carries.
        mode = doc.get("mode")
        notes = doc.get("notes")
        weights = doc.get("weights") or {}
        outcomes_count = doc.get("recent_outcomes_count")
        if outcomes_count is None and isinstance(recent, list):
            outcomes_count = len(recent)

        # Build a contribution-shaped summary.
        if mode:
            bits.append(f"mode={mode}")
        if isinstance(outcomes_count, int) and outcomes_count > 0:
            bits.append(f"outcomes={outcomes_count}")
            if symbol:
                bits.append(f"latest={symbol}")
                if side:
                    bits.append(str(side))
                if conf is not None:
                    try:
                        bits.append(f"conf={float(conf):.2f}")
                    except (TypeError, ValueError):
                        pass
        if isinstance(weights, dict) and weights:
            bits.append(f"weights={len(weights)}")
        if isinstance(notes, str) and notes.strip():
            short = notes.strip()
            if len(short) > 60:
                short = short[:57] + "..."
            bits.append(f'"{short}"')

        # Only flag as skeleton if the audit row truly has nothing.
        # That's the case the 422 empty-contribution gate now prevents
        # — pre-gate historical rows can still surface this.
        has_substance = doc.get("has_substance")
        # Back-compat for rows pre-dating has_substance: compute it.
        if has_substance is None:
            has_substance = bool(
                (isinstance(notes, str) and notes.strip())
                or weights
                or (isinstance(recent, list) and recent)
                or (isinstance(doc.get("delta_reason"), str) and doc["delta_reason"].strip())
                or (doc.get("confidence_delta") or 0) != 0
            )
        if not has_substance and posted_as:
            bits.append("(no substance — pre-gate row)")
    else:
        # Non-contribution sovereign rows: original behaviour.
        if symbol:
            bits.append(symbol)
        if side:
            bits.append(str(side))
        if conf is not None:
            try:
                bits.append(f"conf={float(conf):.2f}")
            except (TypeError, ValueError):
                pass
        if not payload and posted_as:
            bits.append("(empty payload)")

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
    """`mc_shelly` is the MC engine's unified audit log — gate_pass /
    gate_fail / intent_ingested / council_pass / council_block /
    position_opened / position_closed / sidecar packets, etc. These
    are LIVE system events, not training-only signals. The legacy UI
    label `training_signal` was misleading (some emissions WERE
    shadow/training but most are real audit rows from production
    execution). Use `engine_audit` so the label tells the truth."""
    et = doc.get("event_type") or "engine_event"
    return {
        "ts": doc.get("ts") or doc.get("timestamp"),
        "brain": doc.get("brain") or doc.get("runtime"),
        "source_collection": MC_SHELLY,
        "kind": "engine_audit",
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
        description="comma-separated subset: receipt,sovereign_audit,intent,engine_audit "
                    "(legacy alias `training_signal` still accepted for back-compat)",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Unified decisions feed across all output collections.

    Lets the operator answer "what did this brain decide recently?"
    regardless of which collection the engine writes to. Each row
    carries `source_collection` so the operator can see whether a
    given decision came from a receipt, a sovereign-audit, an intent,
    or an MC engine-audit row.
    """
    wanted_kinds = (
        {k.strip() for k in kinds.split(",") if k.strip()}
        if kinds else set()
    )
    # Backward-compat: accept the legacy `training_signal` alias.
    if "training_signal" in wanted_kinds:
        wanted_kinds.discard("training_signal")
        wanted_kinds.add("engine_audit")

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
            MC_SHELLY: "engine_audit",
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
