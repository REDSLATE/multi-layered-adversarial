"""Verifier background loop — periodic attribution writer.

Reads from `execution_receipts` (real broker fills with realised P&L)
and joins them with the BrainVotes that influenced the trade. For each
closed position with a settled outcome:

  * compute pnl_bps from the receipt's realised return,
  * call VerifierReplay.analyze() to attribute the result,
  * persist FailureReason to `paradox_v2_failure_attributions`,
  * feed the outcome back into BrainCalibration.record_outcome() so
    future raw→calibrated shrinkage uses fresh evidence,
  * if the brain that drove the loss was confident, also call
    NegativeKnowledge.learn_from_failure().

Doctrine fit:
  * Read-only on broker / execution side. Never modifies receipts.
  * Calibration + negative-knowledge mutations are SCOPED to in-memory
    brain stores rehydrated from Mongo on demand.
  * Runs OUT OF BAND on a low-frequency cadence (60s default). Never
    in the hot intent path. Trading impact: zero.

The loop is idempotent: receipts that already have an attribution row
are skipped on re-runs. Skipping is keyed by execution_receipt_id.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from db import db
from namespaces import (
    EXECUTION_RECEIPTS, PARADOX_V2_FAILURE_ATTRIBUTIONS,
)
from shared.brain_vote import BrainVote, CalibrationKey
from shared.paradox_v2.vote_doctrine_repo import (
    save_failure_attribution, hydrate_calibration, hydrate_negative_knowledge,
    persist_calibration_outcome, persist_negative_pattern, _doc_to_vote,
)
from brains.calibration import BrainCalibration
from brains.negative_knowledge import NegativeKnowledge
from verifier.replay import VerifierReplay, ReplayCase


logger = logging.getLogger("risedual.paradox_v2.verifier_loop")


SWEEP_INTERVAL_SEC: int = 60
RECEIPT_LOOKBACK_MIN: int = 60  # only grade fills from the last hour


# Hydrated lazily per brain — survives loop iterations.
_CAL_CACHE: dict[str, BrainCalibration] = {}
_NK_CACHE: dict[str, NegativeKnowledge] = {}


async def _get_calibrator(brain_id: str) -> BrainCalibration:
    cal = _CAL_CACHE.get(brain_id)
    if cal is None:
        cal = BrainCalibration(brain_id=brain_id)
        await hydrate_calibration(cal)
        _CAL_CACHE[brain_id] = cal
    return cal


async def _get_negative_knowledge(brain_id: str) -> NegativeKnowledge:
    nk = _NK_CACHE.get(brain_id)
    if nk is None:
        nk = NegativeKnowledge(brain_id=brain_id)
        await hydrate_negative_knowledge(nk)
        _NK_CACHE[brain_id] = nk
    return nk


async def _find_votes_for_receipt(
    receipt: dict[str, Any],
) -> list[BrainVote]:
    """Look up the BrainVotes that influenced this receipt.

    Join heuristic: same symbol, within a 10-minute window before the
    receipt's `executed_at`. Brains emit votes near in time to the
    intent that became the order — this catches the relevant bundle
    without needing an explicit foreign key (votes were originally
    designed as an audit-trail without coupling to receipts).
    """
    from namespaces import PARADOX_V2_BRAIN_VOTES
    sym = (receipt.get("symbol") or "").upper().strip()
    if not sym:
        return []
    executed_at = receipt.get("executed_at")
    if not executed_at:
        return []
    try:
        t = datetime.fromisoformat(str(executed_at).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return []
    earliest = (t - timedelta(minutes=10)).isoformat()
    latest = t.isoformat()
    rows = await db[PARADOX_V2_BRAIN_VOTES].find(
        {"symbol": sym,
         "timestamp": {"$gte": earliest, "$lte": latest}},
        {"_id": 0},
    ).to_list(20)
    return [_doc_to_vote(r) for r in rows]


def _pnl_bps_from_receipt(receipt: dict[str, Any]) -> Optional[float]:
    """Pull realised P&L in bps from a receipt. Receipts may carry P&L
    under any of several keys depending on broker; try each, return
    None if no settled P&L is available yet."""
    for k in ("pnl_bps", "realized_pnl_bps", "realised_pnl_bps"):
        v = receipt.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


async def grade_receipt(
    receipt: dict[str, Any],
    *,
    loss_threshold_bps: int = -50,
) -> Optional[dict[str, Any]]:
    """Grade one receipt: attribute, calibrate, learn. Returns None if
    the receipt isn't settled enough to grade."""
    pnl = _pnl_bps_from_receipt(receipt)
    if pnl is None:
        return None
    direction = (receipt.get("action") or "HOLD").upper()
    if direction not in ("BUY", "SELL", "HOLD"):
        direction = "HOLD"
    votes = await _find_votes_for_receipt(receipt)
    if not votes:
        # No votes recorded → can't attribute. That's a doctrine gap,
        # not a failure — just skip.
        return None

    case = ReplayCase(
        timestamp=datetime.now(timezone.utc),
        symbol=(receipt.get("symbol") or "").upper().strip(),
        regime=str(receipt.get("regime") or "unknown"),
        brain_votes={v.brain: v for v in votes},
        governor_output={},
        roadguard_decision="OPEN",
        seat_action={"direction": direction,
                     "notional_usd": float(receipt.get("notional_usd") or 0.0)},
        actual_outcome={"pnl_bps": pnl},
    )
    reason = VerifierReplay(loss_threshold_bps=loss_threshold_bps).analyze(case)
    aid = await save_failure_attribution(reason, case_context={
        "execution_receipt_id": receipt.get("receipt_id") or receipt.get("intent_id"),
        "symbol": case.symbol, "regime": case.regime,
        "direction": direction, "pnl_bps": pnl,
        "vote_brains": [v.brain for v in votes],
    })

    # Feed the outcome back into every supporting brain's calibration.
    # WON = (BUY + pnl>0) or (SELL + pnl<0). Calibrator records per-key.
    won_by_direction = (direction == "BUY" and pnl > 0) or (direction == "SELL" and pnl < 0)
    for v in votes:
        if v.stance == "ABSTAIN":
            continue
        cal = await _get_calibrator(v.brain)
        # Won from this brain's perspective: their stance agreed with outcome.
        brain_won = (v.stance == "BUY" and pnl > 0) or (v.stance == "SELL" and pnl < 0)
        cal.record_outcome(v.calibration_key, won=brain_won, return_bps=pnl)
        await persist_calibration_outcome(cal, v.calibration_key)

    # If a brain was attributed responsible AND the loss was material,
    # add the setup to its negative-knowledge store.
    if (
        reason.responsible_brain
        and pnl < -100.0
        and reason.type.value == "brain_error"
    ):
        nk = await _get_negative_knowledge(reason.responsible_brain)
        setup_hash = f"{case.symbol}:{case.regime}:{direction}"
        nk.learn_from_failure(setup_hash, regime=case.regime, loss_bps=pnl)
        # Persist the (possibly new) pattern entry.
        patterns = nk.patterns_for_regime(case.regime)
        for p in patterns:
            if p.pattern_hash == setup_hash:
                await persist_negative_pattern(nk, p)
                break

    return {
        "attribution_id": aid,
        "responsible_brain": reason.responsible_brain,
        "type": reason.type.value,
        "pnl_bps": pnl,
        "won_by_direction": won_by_direction,
    }


async def run_one_pass(*, lookback_min: int = RECEIPT_LOOKBACK_MIN) -> dict[str, Any]:
    """One sweep. Find recent settled receipts that don't yet have an
    attribution row, grade them in order."""
    earliest = (datetime.now(timezone.utc) - timedelta(minutes=lookback_min)).isoformat()
    receipts = await db[EXECUTION_RECEIPTS].find(
        {"executed_at": {"$gte": earliest}},
        {"_id": 0},
    ).sort("executed_at", 1).to_list(200)

    # Build set of receipt_ids that already have attributions.
    receipt_ids = [
        r.get("receipt_id") or r.get("intent_id")
        for r in receipts
    ]
    receipt_ids = [r for r in receipt_ids if r]
    existing = await db[PARADOX_V2_FAILURE_ATTRIBUTIONS].find(
        {"case_context.execution_receipt_id": {"$in": receipt_ids}},
        {"case_context.execution_receipt_id": 1, "_id": 0},
    ).to_list(len(receipt_ids))
    already = {
        e.get("case_context", {}).get("execution_receipt_id") for e in existing
    }

    graded = 0
    skipped = 0
    errors: list[str] = []
    for r in receipts:
        rid = r.get("receipt_id") or r.get("intent_id")
        if not rid or rid in already:
            skipped += 1
            continue
        try:
            out = await grade_receipt(r)
            if out is None:
                skipped += 1
            else:
                graded += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{rid}: {e}")

    return {"graded": graded, "skipped": skipped, "errors": errors,
            "lookback_min": lookback_min,
            "ts": datetime.now(timezone.utc).isoformat()}


# ─── async background driver ──────────────────────────────────────────


_STOP = asyncio.Event()


async def _driver(interval_sec: int) -> None:
    while not _STOP.is_set():
        try:
            await run_one_pass()
        except Exception as e:  # noqa: BLE001
            logger.warning("verifier_loop pass failed: %s", e)
        try:
            await asyncio.wait_for(_STOP.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass


async def start_verifier_loop(interval_sec: int = SWEEP_INTERVAL_SEC) -> None:
    _STOP.clear()
    asyncio.create_task(_driver(interval_sec))
    logger.info("paradox_v2 verifier_loop started interval=%ss", interval_sec)


async def stop_verifier_loop() -> None:
    _STOP.set()
