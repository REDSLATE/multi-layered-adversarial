"""Counterfactual signals — durable learning artefacts distilled from
blocked directional intents.

Doctrine (2026-02-19, operator directive):

    Executed trades       → "Did the trade work?"
    Blocked trade signals → "Would the trade have worked?"

    Every stale directional intent that never reached the broker
    is distilled into ONE compact `counterfactual_signals` row
    BEFORE the raw intent is deleted. That row is then resolved
    over time (5m, 15m, 1h) against live mark prices, producing
    a verdict:

        MISSED_WIN     — direction was right, block cost us edge
        CORRECT_BLOCK  — direction was wrong, block saved us
        UNDETERMINED   — noise, |bps| < threshold

    The signal never goes back to the broker. It's learning
    evidence only, feeding the bucket analyzer and Kernel.

    Turns the purge problem into a learning system: the raw intent
    dies but the signal it carried survives in compact form.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("shared.counterfactuals.signals")

COUNTERFACTUAL_SIGNALS = "counterfactual_signals"

# Verdict thresholds (bps). ±20 bps is a strong-enough move that
# either "the trade would have worked" or "the trade would have
# hurt" — anything between is noise.
VERDICT_WIN_BPS = 20.0
VERDICT_LOSS_BPS = -20.0

# Session horizon length — used when we can't get a live mark, we
# still stamp a session-close outcome from bars.
HORIZONS_SEC: dict[str, int] = {
    "5m":  5 * 60,
    "15m": 15 * 60,
    "1h":  60 * 60,
}

_DIRECTIONAL = {"BUY", "SELL", "SHORT", "COVER"}
_BLOCKED_GATE_STATES = {"no_trade", "blocked", "expired_unrouted"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def should_create_counterfactual(intent: dict) -> bool:
    """Operator's conversion predicate — only intents that carried a
    real proposed direction get distilled.

    True iff:
        action ∈ {BUY, SELL, SHORT, COVER}
        AND not executed
        AND no broker_order_id
        AND gate_state ∈ {no_trade, blocked, expired_unrouted}
    """
    exec_block = intent.get("execution") or {}
    action_raw = (
        (exec_block.get("action") if isinstance(exec_block, dict) else None)
        or intent.get("action") or ""
    )
    action = str(action_raw).upper()
    if action not in _DIRECTIONAL:
        return False
    if intent.get("executed") is True:
        return False
    if intent.get("broker_order_id"):
        return False
    gate_state = str(intent.get("gate_state") or "").lower()
    return gate_state in _BLOCKED_GATE_STATES


def _extract_block_reason(intent: dict) -> Optional[str]:
    """Best-effort block-reason lookup — different emitters stamp
    different fields."""
    for k in (
        "broker_reason", "block_reason", "gate_reason", "reason",
        "verdict_reason",
    ):
        v = intent.get(k)
        if v:
            return str(v)
    ev = intent.get("evidence") or {}
    if isinstance(ev, dict):
        for k in ("gate_reason", "block_reason", "reason"):
            v = ev.get(k)
            if v:
                return str(v)
    return None


def _extract_reference_price(intent: dict) -> Optional[float]:
    """Reference price at the moment the intent was created. Tried
    in order: `snapshot.price` → `target_price` → `execution.price`.
    """
    snapshot = intent.get("snapshot") or {}
    for candidate in (
        snapshot.get("price") if isinstance(snapshot, dict) else None,
        snapshot.get("last") if isinstance(snapshot, dict) else None,
        intent.get("target_price"),
        (intent.get("execution") or {}).get("price"),
    ):
        if candidate is None:
            continue
        try:
            v = float(candidate)
        except (TypeError, ValueError):
            continue
        if v > 0.0:
            return v
    return None


def _extract_features(intent: dict) -> dict:
    """Pull the feature vector the bucket analyzer will key on."""
    snap = intent.get("snapshot") or {}
    if not isinstance(snap, dict):
        snap = {}
    features_present = intent.get("features") or {}
    if not isinstance(features_present, dict):
        features_present = {}

    def _pick(k):
        return snap.get(k) if snap.get(k) is not None else features_present.get(k)

    return {
        "relative_volume": _pick("relative_volume"),
        "rvol_acceleration": _pick("rvol_acceleration"),
        "vwap_distance_pct": _pick("vwap_distance_pct"),
        "velocity_5m": _pick("velocity_5m"),
        "market_regime": _pick("market_regime"),
        "spread_bps": _pick("spread_bps"),
    }


async def distill_intent_to_signal(intent: dict, db) -> bool:
    """Write ONE compact `counterfactual_signals` row for `intent`.

    Idempotent via `$setOnInsert` on `signal_id == intent_id`, so
    re-invocation on the same intent is safe.

    Returns True iff a signal exists after the call (either freshly
    inserted OR already present). Returns False if the intent
    doesn't qualify, or if the reference price is missing (we
    refuse to distill a signal we can't score).
    """
    if not should_create_counterfactual(intent):
        return False

    intent_id = intent.get("intent_id")
    if not intent_id:
        return False

    exec_block = intent.get("execution") or {}
    action = str(
        (exec_block.get("action") if isinstance(exec_block, dict) else None)
        or intent.get("action") or ""
    ).upper()

    reference_price = _extract_reference_price(intent)
    if reference_price is None:
        # No entry mark — we can't score this signal. Return False
        # so the caller preserves the raw intent instead of deleting
        # what carried the signal.
        return False

    doc = {
        "signal_id": intent_id,
        "source_intent_id": intent_id,
        # `experience_type` dimension lets the bucket analyzer key
        # counterfactuals separately from executed learning
        # experiences — "did the trade work?" vs "would it have
        # worked?" must never share a bucket blindly.
        "experience_type": "counterfactual",
        "brain": intent.get("stack_canonical") or intent.get("stack"),
        "symbol": intent.get("symbol"),
        "lane": (intent.get("lane") or "").lower() or None,
        "direction": action,
        "entry_reference_price": float(reference_price),
        "blocked_reason": _extract_block_reason(intent),
        "features": _extract_features(intent),
        "status": "tracking",
        "outcomes": {},   # populated by the resolver over time
        "created_at": intent.get("ingest_ts") or _iso(_now()),
        "distilled_at": _iso(_now()),
        # ── Permanent execution firewall ────────────────────────────
        # These signals are learning evidence ONLY. They must never
        # be re-routed to the broker or grow legs of their own.
        # Belt-and-suspenders — every downstream consumer that reads
        # this collection MUST also honour these fields, and any
        # code that queries this collection to build execution
        # payloads is a doctrine violation.
        "may_execute": False,
        "broker_access": False,
    }
    try:
        await db[COUNTERFACTUAL_SIGNALS].update_one(
            {"signal_id": intent_id},
            {"$setOnInsert": doc},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "counterfactuals: distill upsert failed intent_id=%s: %s",
            intent_id, exc,
        )
        return False
    return True


# ═══════════════════════════════════════════════════════════════════
#  Resolver — score signals as market moves
# ═══════════════════════════════════════════════════════════════════


def signed_return_bps(
    direction: str, entry_price: float, mark_price: float,
) -> float:
    """Signed bps of counterfactual return. Positive = direction was
    right (would have worked)."""
    if entry_price <= 0:
        return 0.0
    raw = (mark_price - entry_price) / entry_price * 10_000.0
    if direction.upper() in {"SELL", "SHORT"}:
        return -raw
    return raw


def _verdict_for(bps: float) -> str:
    if bps >= VERDICT_WIN_BPS:
        return "MISSED_WIN"
    if bps <= VERDICT_LOSS_BPS:
        return "CORRECT_BLOCK"
    return "UNDETERMINED"


async def resolve_pending_signals(db) -> dict:
    """Walk `counterfactual_signals` where at least one horizon is
    unresolved AND enough time has passed, then stamp outcomes.

    Uses the same fresh-mark-quote resolver as the executed-trade
    outcome resolver. Never raises.
    """
    counts = {
        "scanned": 0,
        "resolved_5m": 0, "resolved_15m": 0, "resolved_1h": 0,
        "skipped_no_mark": 0,
        "skipped_stale_mark": 0,
        "errors": 0,
    }
    now = _now()
    # Ripe = older than earliest horizon (5m).
    ripe_cutoff = _iso(now - timedelta(seconds=HORIZONS_SEC["5m"]))

    from shared.learning.outcome_resolver import _fetch_mark_quote  # noqa: WPS433

    try:
        cur = (
            db[COUNTERFACTUAL_SIGNALS]
            .find(
                {
                    "created_at": {"$lt": ripe_cutoff},
                    "status": "tracking",
                    "$or": [
                        {"outcomes.5m": {"$exists": False}},
                        {"outcomes.15m": {"$exists": False}},
                        {"outcomes.1h": {"$exists": False}},
                    ],
                },
                {"_id": 0},
            )
            .sort("created_at", 1)
            .limit(500)
        )
        rows: list[dict] = []
        async for r in cur:
            rows.append(r)
    except Exception as exc:  # noqa: BLE001
        logger.warning("counterfactuals: resolve query failed: %s", exc)
        return counts

    for sig in rows:
        counts["scanned"] += 1
        signal_id = sig.get("signal_id")
        direction = str(sig.get("direction") or "").upper()
        entry = sig.get("entry_reference_price")
        try:
            entry_price = float(entry) if entry is not None else 0.0
        except (TypeError, ValueError):
            counts["errors"] += 1
            continue
        if entry_price <= 0:
            counts["errors"] += 1
            continue

        try:
            created = datetime.fromisoformat(
                str(sig["created_at"]).replace("Z", "+00:00"),
            )
        except (KeyError, ValueError, AttributeError):
            counts["errors"] += 1
            continue
        age_s = (now - created).total_seconds()

        outcomes = sig.get("outcomes") or {}
        pending: list[tuple[str, int]] = []
        for h, sec in HORIZONS_SEC.items():
            if age_s < sec:
                break
            if h in outcomes:
                continue
            pending.append((h, sec))
        if not pending:
            continue

        quote = await _fetch_mark_quote(sig.get("lane"), sig.get("symbol"))
        if quote is None:
            counts["skipped_no_mark"] += 1
            continue
        if quote.is_stale:
            counts["skipped_stale_mark"] += 1
            continue

        bps = signed_return_bps(direction, entry_price, quote.price)
        verdict = _verdict_for(bps)

        set_payload: dict[str, Any] = {}
        resolved_at = _iso(now)
        for h, _sec in pending:
            set_payload[f"outcomes.{h}.mark_price"] = quote.price
            set_payload[f"outcomes.{h}.return_bps"] = round(bps, 2)
            set_payload[f"outcomes.{h}.verdict"] = verdict
            set_payload[f"outcomes.{h}.mark_source"] = quote.source
            set_payload[f"outcomes.{h}.resolved_at"] = resolved_at
            counts[f"resolved_{h}"] += 1

        # When the 1h horizon lands, freeze the signal.
        if any(h == "1h" for h, _ in pending):
            set_payload["status"] = "resolved"
            set_payload["final_verdict"] = verdict
            set_payload["final_return_bps"] = round(bps, 2)

        try:
            await db[COUNTERFACTUAL_SIGNALS].update_one(
                {"signal_id": signal_id},
                {"$set": set_payload},
            )
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.warning(
                "counterfactuals: outcome write failed signal_id=%s: %s",
                signal_id, exc,
            )

    if counts["scanned"]:
        logger.info(
            "counterfactuals.resolver: scanned=%d resolved(5m=%d 15m=%d 1h=%d) "
            "no_mark=%d stale=%d errors=%d",
            counts["scanned"], counts["resolved_5m"], counts["resolved_15m"],
            counts["resolved_1h"], counts["skipped_no_mark"],
            counts["skipped_stale_mark"], counts["errors"],
        )
    return counts
