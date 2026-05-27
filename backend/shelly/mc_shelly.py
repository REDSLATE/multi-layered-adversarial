"""MC Shelly — head memory across all brain Shellys. SYNC pymongo.

Doctrine:
    MCShelly aggregates rollups from each LocalShelly. It reasons
    OVER THE WHOLE FLEET, not any one brain.

    MCShelly has the same authority ceiling as LocalShelly: memory
    + reasoning only. Every artifact carries
    `authority="memory_reasoning_only"`.

Storage:
    shelly_mc_shared_memory          — deduped union of all brain rollups
    shelly_mc_reasoning_receipts     — cross-brain reasoning verdicts

Coexistence note:
    `shared/mc_shelly.py` (existing — generic event audit log) is
    UNRELATED to this module. That one uses the `mc_shelly` collection.
    This module uses `shelly_mc_shared_*`. They coexist; no migration.
"""
from __future__ import annotations

from typing import Any

from shelly.contracts import (
    AUTHORITY_MEMORY_REASONING_ONLY,
    RECOMMENDATIONS_ALLOWED,
    ShellyReasoningReceipt,
    stable_hash,
    utc_now,
)
from shelly.sync_db import get_db


MC_SIMILAR_LOOKBACK = 100
MC_MIN_SAMPLES_FOR_VERDICT = 10
MC_LOSS_RATE_WARN_THRESHOLD = 0.60
MC_LOSS_RATE_SUPPORT_THRESHOLD = 0.35


class MCShelly:
    """Head Shelly. One instance for the whole fleet."""

    SHARED_MEMORY_COLL = "shelly_mc_shared_memory"
    RECEIPTS_COLL = "shelly_mc_reasoning_receipts"

    @property
    def shared(self):
        return get_db()[self.SHARED_MEMORY_COLL]

    @property
    def receipts(self):
        return get_db()[self.RECEIPTS_COLL]

    # ──────────────────────── rollup ingest ────────────────────────

    def ingest_rollup(
        self, brain: str, memories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Receive a batch of memory events from a LocalShelly's
        rollup. Dedupe by event_hash. Idempotent."""
        inserted = 0
        duplicates = 0

        for memory in memories:
            memory = dict(memory)
            memory.pop("_id", None)

            memory["source_brain"] = brain.lower()
            memory["shelly_scope"] = "mc_shared"
            memory["ingested_at_mc"] = utc_now()
            # Re-stamp the authority tag at the boundary so a tampered
            # rollup cannot smuggle in a different value.
            memory["authority"] = AUTHORITY_MEMORY_REASONING_ONLY

            result = self.shared.update_one(
                {"event_hash": memory["event_hash"]},
                {"$setOnInsert": memory},
                upsert=True,
            )
            if result.upserted_id is not None:
                inserted += 1
            else:
                duplicates += 1

        return {
            "ok": True,
            "brain": brain.lower(),
            "inserted": inserted,
            "duplicates": duplicates,
            "authority": AUTHORITY_MEMORY_REASONING_ONLY,
        }

    # ──────────────────────── cross-brain reasoning ────────────────────────

    def reason_across_shellys(
        self, current_case: dict[str, Any],
    ) -> dict[str, Any]:
        """Pull similar resolved cases across ALL brains, compute
        fleet-wide loss rate, emit verdict. Advisory only."""
        symbol = current_case.get("symbol")
        direction = current_case.get("direction")

        matches = list(
            self.shared.find(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "outcome.pnl_pct": {"$exists": True},
                },
                {"_id": 0},
            )
            .sort("created_at", -1)
            .limit(MC_SIMILAR_LOOKBACK)
        )

        by_brain: dict[str, dict[str, int]] = {}
        for m in matches:
            brain = m.get("source_brain", "unknown")
            pnl = float(m.get("outcome", {}).get("pnl_pct", 0))
            slot = by_brain.setdefault(
                brain, {"wins": 0, "losses": 0, "total": 0},
            )
            slot["total"] += 1
            if pnl > 0:
                slot["wins"] += 1
            elif pnl < 0:
                slot["losses"] += 1

        total = sum(v["total"] for v in by_brain.values())
        losses = sum(v["losses"] for v in by_brain.values())

        reasons: list[str] = []
        confidence_delta = 0.0
        recommendation = "neutral"

        if total >= MC_MIN_SAMPLES_FOR_VERDICT:
            loss_rate = losses / total
            if loss_rate >= MC_LOSS_RATE_WARN_THRESHOLD:
                recommendation = "warn"
                confidence_delta = -0.20
                reasons.append(
                    f"Fleet-wide loss rate {loss_rate:.0%} across "
                    f"{total} cases ({len(by_brain)} brains)."
                )
            elif loss_rate <= MC_LOSS_RATE_SUPPORT_THRESHOLD:
                recommendation = "support"
                confidence_delta = 0.10
                reasons.append(
                    f"Fleet-wide loss rate only {loss_rate:.0%} across "
                    f"{total} cases — historically favorable."
                )
            else:
                reasons.append(
                    f"Fleet-wide loss rate {loss_rate:.0%} — mixed."
                )
        else:
            reasons.append(
                f"Only {total} shared cases; need ≥{MC_MIN_SAMPLES_FOR_VERDICT} "
                f"before MC Shelly forms a verdict."
            )

        # Brain disagreement detection — informational, not authority.
        brain_loss_rates = [
            (v["losses"] / v["total"]) if v["total"] > 0 else 0.0
            for v in by_brain.values()
        ]
        has_conflict = False
        if len(brain_loss_rates) >= 2:
            has_conflict = (
                max(brain_loss_rates) - min(brain_loss_rates) >= 0.40
            )
            if has_conflict:
                reasons.append(
                    "Brains disagree on this setup: spread between brain "
                    f"loss rates is {(max(brain_loss_rates) - min(brain_loss_rates)):.0%}."
                )

        receipt = ShellyReasoningReceipt(
            brain="mc_shelly",
            symbol=symbol or "UNKNOWN",
            recommendation=recommendation,
            confidence_delta=confidence_delta,
            reasons=reasons,
            evidence_hashes=[m["event_hash"] for m in matches[:20]],
        ).to_doc()
        receipt["direction"] = direction
        receipt["by_brain"] = by_brain
        receipt["has_brain_conflict"] = has_conflict
        receipt["receipt_hash"] = stable_hash(
            {k: v for k, v in receipt.items() if k != "receipt_hash"}
        )

        self.receipts.insert_one(receipt)
        receipt.pop("_id", None)
        return receipt

    # ──────────────────────── self-check ────────────────────────

    @staticmethod
    def doctrine_authority_tag() -> str:
        return AUTHORITY_MEMORY_REASONING_ONLY

    @staticmethod
    def allowed_recommendations() -> frozenset[str]:
        return RECOMMENDATIONS_ALLOWED
