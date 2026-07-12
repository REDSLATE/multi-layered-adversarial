"""PulseReceipt — orchestration health, separate from brain health.

Design freeze: `MC_PULSE.md` §7. "Pulse healthy" MUST require
`brains_failed == []`. A green pulse hiding a dead brain is the
exact 3-clock dishonesty we've eliminated across the system.

Persistence:
    * `mc_pulses` collection — `_id = pulse_id`, TTL 7 days.
    * `brain_runtime_metrics.risedual_stack.pulse.*` — mirror of
      the latest receipt for the operator dashboard (single-doc
      read, no aggregation).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from db import db

MC_PULSES = "mc_pulses"
BRM = "brain_runtime_metrics"
STACK_ID = "risedual_stack"


@dataclass
class BrainFailure:
    """One row per brain that timed out or threw during a pulse.
    Exception type is captured but not the traceback (the log
    stream already has that — the receipt is for dashboards, not
    forensics)."""
    brain_id: str
    reason: str          # "evaluation_timeout" | "evaluation_error"
    exc_type: Optional[str] = None
    symbol: Optional[str] = None
    lane: Optional[str] = None
    at: str = ""


@dataclass
class BrainSilence:
    """One row per (brain, snapshot) pair where the brain contributed
    nothing to a pulse — but for a KNOWN, non-error reason.

    Added 2026-02-11 (P1: no_data reason-code breakdown). The
    aggregate `no_data_rate` in `pulse_health_routes` used to be
    a single scalar with no explanation. Now we stamp a per-brain
    silence reason at the point the pulse loop decides to skip
    that brain, and the health tile can slice it operator-side:

        no_data 61.2%
          32.4%  snapshot_stale
          14.1%  cadence_cooldown
           2.7%  no_signal_return

    Reason vocabulary (kept small and stable — the tile hard-codes
    the labels):

      * `snapshot_stale`   — the snapshot's freshness gate rejected
        it before it reached the brain (market closed / stale feed
        / missing bar). This is the operator's "market_closed" +
        "stale_feed" bucket collapsed to the actual system signal.
      * `cadence_cooldown` — the brain declined to evaluate because
        `should_evaluate` returned False (already looked at this
        symbol within CADENCE_SECONDS). Expected, not a health
        problem — but useful to distinguish from real gaps.
      * `no_signal_return` — the brain ran evaluate() and returned
        None (direction mapping failed, or strategy returned an
        unmappable action). Rare; usually indicates a strategy
        bug worth surfacing.

    NOT captured here (deliberately):
      * `INSUFFICIENT_DATA` opinions — the brain ACTUALLY spoke,
        it just said "I don't know". Those are opinions on the
        tape and counted separately by `stale_input_rate`.
      * `BrainFailure` — exceptions belong on `brains_failed`.
    """
    brain_id: str
    reason: str          # snapshot_stale | cadence_cooldown | no_signal_return
    symbol: Optional[str] = None
    lane: Optional[str] = None


@dataclass
class PulseReceipt:
    """One document per pulse. Written twice — once at
    `begin_pulse` (start marker, so a crashed pulse is still
    visible), once at `complete_pulse` (final health)."""
    pulse_id: str
    started_at: str                        # ISO-8601 UTC
    completed_at: Optional[str] = None
    cadence_seconds: int = 0
    snapshot_count: int = 0
    brains_expected: int = 0
    brains_completed: list[str] = field(default_factory=list)
    brains_failed: list[BrainFailure] = field(default_factory=list)
    # Per-brain silences with a KNOWN reason (P1 no_data breakdown).
    # See `BrainSilence` docstring for the reason vocabulary.
    brains_silent: list[BrainSilence] = field(default_factory=list)
    arbitrations_completed: int = 0
    intents_emitted: int = 0                # 0 while DISARMED
    grader_enqueued: int = 0
    runtime_mode: str = "DISARMED"
    overrun: bool = False

    @property
    def orchestration_ok(self) -> bool:
        """Green ONLY when every expected brain returned AND we
        didn't miss the next cadence window. Design freeze §7:
        never conflate pulse and brain health."""
        return (
            self.completed_at is not None
            and len(self.brains_failed) == 0
            and not self.overrun
        )

    def to_mongo(self) -> dict:
        d = asdict(self)
        # brains_failed dataclasses need explicit conversion
        d["brains_failed"] = [
            asdict(bf) if hasattr(bf, "__dataclass_fields__") else bf
            for bf in self.brains_failed
        ]
        d["brains_silent"] = [
            asdict(bs) if hasattr(bs, "__dataclass_fields__") else bs
            for bs in self.brains_silent
        ]
        d["_id"] = self.pulse_id
        d["orchestration_ok"] = self.orchestration_ok
        return d


async def persist_receipt(receipt: PulseReceipt) -> None:
    """Upsert into `mc_pulses` and mirror onto the stack doc.

    Mirroring is best-effort — a Motor error MUST NOT bubble into
    the pulse loop. The stack-doc mirror is a convenience for the
    dashboard; source of truth is `mc_pulses`."""
    doc = receipt.to_mongo()
    try:
        await db[MC_PULSES].update_one(
            {"_id": receipt.pulse_id},
            {"$set": doc},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("mc_pulse.receipt").warning(
            "persist_receipt write failed pulse_id=%s: %s",
            receipt.pulse_id, exc,
        )
    try:
        await db[BRM].update_one(
            {"_id": STACK_ID},
            {
                "$set": {
                    "pulse.latest": doc,
                    "pulse.latest_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
                "$setOnInsert": {
                    "_id": STACK_ID,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("mc_pulse.receipt").warning(
            "persist_receipt mirror failed pulse_id=%s: %s",
            receipt.pulse_id, exc,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
