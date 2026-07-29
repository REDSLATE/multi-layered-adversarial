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
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db

MC_PULSES = "mc_pulses"
# P1 (2026-02-11): per-silence-row diagnostic log. `mc_pulses` stores
# the array of `BrainSilence` rows inline on each pulse doc, which is
# fine for the aggregate breakdown on the health tile — but useless
# for ad-hoc questions like "show me every pulse where camino was
# silent on NVDA with reason=snapshot_stale in the last week". This
# collection holds one flat row per (pulse, brain, snapshot) silence,
# indexed for point-lookup, with a TTL sweep so it doesn't grow
# unbounded. Aggregate metrics DO NOT read from here — this is a
# diagnostic sidecar.
MC_BRAIN_SILENCES = "mc_brain_silences"
# Days before a silence row is auto-purged. Matches the operator's
# working memory: the tile shows a 24h window, week gives room to
# investigate incidents surfaced during the workweek.
MC_BRAIN_SILENCES_TTL_DAYS = 7
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
    # 2026-07-20 (kill-map Phase 1): per-brain envelope counts this
    # tick, e.g. {"camino": 12, "gto": 9}. Answers "did the
    # adversarial core get to compete?" without unwinding mc_seats.
    opinions_by_brain: dict = field(default_factory=dict)
    # 2026-07-20 (kill-map Phase 1): emission-suppression tally.
    # Keys: emitted · suppressed_disarmed · emit_error · no_opinions
    # · all_flat. Previously these outcomes were computed and thrown
    # away — the exact "arbiter runs but nothing comes out" blind spot.
    arbitration_outcomes: dict = field(default_factory=dict)
    grader_enqueued: int = 0
    runtime_mode: str = "DISARMED"
    overrun: bool = False
    # 2026-07-15 (iter-30 P2): captured exception summary when the
    # pulse orchestrator itself throws (as opposed to a brain failing).
    # Written as `"{ExceptionClass}: {message}"` (message truncated).
    # None on healthy pulses. When set, `orchestration_ok` is False —
    # this is the ONE place a red ⚠ on the operator health strip can
    # be traced back to an actionable message instead of "something".
    orchestration_error: Optional[str] = None

    @property
    def orchestration_ok(self) -> bool:
        """Green ONLY when every expected brain returned AND we
        didn't miss the next cadence window AND no orchestrator
        exception was captured. Design freeze §7: never conflate
        pulse and brain health."""
        return (
            self.completed_at is not None
            and len(self.brains_failed) == 0
            and not self.overrun
            and self.orchestration_error is None
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

    # P1 (2026-02-11): sidecar log of individual silence rows for
    # ad-hoc diagnostics. Best-effort — a Motor error MUST NOT bubble
    # into the pulse loop. If the collection is missing indexes /
    # TTL, we still write; the migration script wires those up.
    if receipt.brains_silent:
        try:
            silence_docs = []
            # BSON-Date TTL stamp (2026-07-29): the TTL on the ISO-
            # string `at` never reaped — Date fields only.
            _ttl_at = datetime.now(timezone.utc) + timedelta(days=7)
            for bs in receipt.brains_silent:
                bs_dict = asdict(bs) if hasattr(bs, "__dataclass_fields__") else dict(bs)
                bs_dict["pulse_id"] = receipt.pulse_id
                bs_dict["at"] = receipt.completed_at or _now_iso()
                bs_dict["ttl_at"] = _ttl_at
                silence_docs.append(bs_dict)
            await db[MC_BRAIN_SILENCES].insert_many(silence_docs, ordered=False)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger("mc_pulse.receipt").warning(
                "persist_receipt silence log write failed pulse_id=%s: %s",
                receipt.pulse_id, exc,
            )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
