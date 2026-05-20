"""
Memory Kernel — P0
==================

Append-only memory ledger with provenance classification, settlement-oracle
consensus, capability-routing gate, and atomic fetch-and-lock training.

Core rule installed by this module:

    Shelly can submit memory.
    MC classifies memory.
    Only VE trains.
    No stack self-certifies VE.

The four provenance classes:

    VE — Verified Execution      (only class allowed into training)
    SO — Simulation / Replay     (replay engine fodder, never training)
    DI — Diagnostic / Informational (notes, dissent, governance reviews)
    UV — Unverified / Quarantined  (anything that failed consensus)

The axiom in `confirm_training_complete()` is load-bearing — it is the one
line that must never change:

    if memory_record["provenance"] != Provenance.VE.value:
        raise RuntimeError("Refusing to train on non-verified memory")
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stable_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Provenance(str, Enum):
    VE = "VE"  # Verified Execution
    SO = "SO"  # Simulation / Replay Only
    DI = "DI"  # Diagnostic / Informational
    UV = "UV"  # Unverified / Quarantined


class SettlementOracle:
    """
    MC-only settlement verifier.

    No stack / shelly can self-certify VE.
    VE requires broker + internal-receipt agreement on:
      - symbol
      - filled status
      - filled_qty (and optionally expected_qty)

    Clearinghouse confirmation is optional in P0; the consensus surface is
    deliberately small so it can be hardened by adding new sources without
    changing the contract.
    """

    def __init__(self, db):
        self.db = db

    async def verify(
        self,
        *,
        symbol: Optional[str],
        broker_order_id: Optional[str],
        execution_receipt_id: Optional[str],
        expected_qty: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not broker_order_id or not execution_receipt_id:
            return {
                "settled": False,
                "reason": "missing_broker_or_receipt_id",
                "sources": {},
            }

        broker = await self.db.broker_orders.find_one({"broker_order_id": broker_order_id})
        receipt = await self.db.execution_receipts.find_one({"receipt_id": execution_receipt_id})

        sources = {
            "broker": broker,
            "internal_receipt": receipt,
        }

        if not broker or not receipt:
            return {
                "settled": False,
                "reason": "source_missing",
                "sources": sources,
            }

        broker_symbol = broker.get("symbol")
        receipt_symbol = receipt.get("symbol")
        broker_status = str(broker.get("status", "")).upper()
        receipt_status = str(receipt.get("status", "")).upper()

        broker_qty = float(broker.get("filled_qty") or 0)
        receipt_qty = float(receipt.get("filled_qty") or 0)

        symbol_match = broker_symbol == symbol and receipt_symbol == symbol
        filled = broker_status in {"FILLED", "SETTLED"} and receipt_status in {"FILLED", "SETTLED"}
        qty_match = broker_qty > 0 and broker_qty == receipt_qty

        if expected_qty is not None:
            qty_match = qty_match and broker_qty == float(expected_qty)

        consensus = {
            "symbol_match": symbol_match,
            "filled": filled,
            "qty_match": qty_match,
            "broker_qty": broker_qty,
            "receipt_qty": receipt_qty,
        }

        settled = bool(symbol_match and filled and qty_match)

        result = {
            "settled": settled,
            "reason": "consensus_ok" if settled else "consensus_failure",
            "sources": sources,
            "consensus": consensus,
        }
        result["consensus_hash"] = stable_hash(result)
        return result


class MemoryKernelLedger:
    """
    Append-only memory ledger.

    Main invariant: only MC may classify VE.

    Routes:
        execution / paper_execution / trade_execution → SettlementOracle → VE | UV
        simulation / replay / backtest                 → SO
        diagnostic / critique / governance / dissent   → DI
        anything else                                  → UV
    """

    def __init__(self, db):
        self.db = db
        self.oracle = SettlementOracle(db)

    async def reclassify_uv_to_so(
        self,
        *,
        memory_id: str,
        operator: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Promote a single quarantined UV memory to SO (replay-only).

        SO memories may feed the replay engine, backtester, and
        adversarial critic — but they are still NOT trainable. The
        load-bearing axiom in `confirm_training_complete` continues to
        refuse anything that is not VE.

        The original quarantine row is preserved (append-only).
        Reclassification writes a new entry to
        `memory_kernel_reclassifications` for permanent audit.
        """
        if not reason or not reason.strip():
            raise ValueError("reclassification reason required")
        if not operator:
            raise ValueError("operator id required")

        mem = await self.db.memory_kernel_ledger.find_one({"memory_id": memory_id})
        if not mem:
            raise ValueError(f"memory not found: {memory_id}")

        current = mem.get("provenance")
        if current == Provenance.SO.value:
            # Idempotent: already SO. No-op but report the prior state.
            return {
                "ok": True,
                "memory_id": memory_id,
                "from": current,
                "to": Provenance.SO.value,
                "no_op": True,
            }
        if current != Provenance.UV.value:
            # Doctrine: only UV → SO is allowed. UV → VE is forbidden.
            # VE → anything is forbidden (already verified, append-only).
            # DI / SO → anything is forbidden (each lane locks the data).
            raise PermissionError(
                f"reclassification refused: only UV→SO is allowed "
                f"(memory currently {current})"
            )

        # Append-only audit record.
        audit = {
            "reclassification_id": str(uuid.uuid4()),
            "memory_id": memory_id,
            "from_provenance": Provenance.UV.value,
            "to_provenance": Provenance.SO.value,
            "operator": operator,
            "reason": reason.strip(),
            "created_at": utc_now(),
            "memory_type": mem.get("memory_type"),
            "source_stack": mem.get("source_stack"),
            "payload_hash": mem.get("payload_hash"),
        }
        await self.db.memory_kernel_reclassifications.insert_one(audit)

        # Update the ledger row. `trainable` stays False (SO is not VE).
        # We deliberately do NOT touch `used_in_training`, payload, or
        # `append_only` — the original facts are immutable.
        await self.db.memory_kernel_ledger.update_one(
            {"memory_id": memory_id},
            {"$set": {
                "provenance": Provenance.SO.value,
                "trainable": False,
                "reclassified_from": Provenance.UV.value,
                "reclassified_to": Provenance.SO.value,
                "reclassification_id": audit["reclassification_id"],
                "reclassified_at": utc_now(),
                "updated_at": utc_now(),
            }},
        )

        return {
            "ok": True,
            "memory_id": memory_id,
            "from": Provenance.UV.value,
            "to": Provenance.SO.value,
            "reclassification_id": audit["reclassification_id"],
        }

    async def submit_memory(
        self,
        *,
        source_stack: str,
        memory_type: str,
        payload: Dict[str, Any],
        requested_provenance: Optional[str] = None,
    ) -> Dict[str, Any]:
        memory_id = str(uuid.uuid4())

        # Note: classify mutates payload by attaching the oracle proof so
        # forensic queries can recover the consensus snapshot later.
        provenance = await self._classify(
            memory_type=memory_type,
            payload=payload,
            requested_provenance=requested_provenance,
        )

        doc = {
            "memory_id": memory_id,
            "source_stack": source_stack,
            "memory_type": memory_type,
            "payload": payload,
            "requested_provenance": requested_provenance,
            "provenance": provenance.value,
            "trainable": provenance == Provenance.VE,
            "used_in_training": False,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "append_only": True,
            "payload_hash": stable_hash(payload),
        }

        await self.db.memory_kernel_ledger.insert_one(doc)

        if provenance == Provenance.UV:
            await self.db.memory_kernel_quarantine.insert_one({
                "memory_id": memory_id,
                "source_stack": source_stack,
                "reason": "classified_uv",
                "created_at": utc_now(),
                "payload_hash": doc["payload_hash"],
            })

        return doc

    async def _classify(
        self,
        *,
        memory_type: str,
        payload: Dict[str, Any],
        requested_provenance: Optional[str],
    ) -> Provenance:
        mt = str(memory_type or "").lower()

        if mt in {"simulation", "replay", "backtest"}:
            return Provenance.SO

        if mt in {"diagnostic", "critique", "governance_review", "council_dissent"}:
            return Provenance.DI

        if mt in {"execution", "trade_execution", "paper_execution"}:
            proof = await self.oracle.verify(
                symbol=payload.get("symbol"),
                broker_order_id=payload.get("broker_order_id"),
                execution_receipt_id=payload.get("execution_receipt_id"),
                expected_qty=payload.get("filled_qty"),
            )
            payload["settlement_oracle"] = proof
            return Provenance.VE if proof.get("settled") is True else Provenance.UV

        return Provenance.UV

    async def append_route_decision(
        self,
        *,
        memory_id: str,
        from_component: str,
        to_component: str,
        route_allowed: bool,
        reason: str,
    ) -> Dict[str, Any]:
        doc = {
            "route_id": str(uuid.uuid4()),
            "memory_id": memory_id,
            "from_component": from_component,
            "to_component": to_component,
            "route_allowed": route_allowed,
            "reason": reason,
            "created_at": utc_now(),
        }
        await self.db.memory_kernel_routes.insert_one(doc)
        return doc

    async def fetch_and_lock_trainable(
        self,
        *,
        min_samples: int = 20,
        limit: int = 200,
        lock_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        lock_id = lock_id or str(uuid.uuid4())

        cursor = self.db.memory_kernel_ledger.find({
            "provenance": Provenance.VE.value,
            "trainable": True,
            "used_in_training": False,
            "training_lock": {"$exists": False},
        }).limit(limit)

        docs = await cursor.to_list(length=limit)

        if len(docs) < min_samples:
            return {
                "ok": False,
                "reason": "insufficient_ve_samples",
                "count": len(docs),
                "lock_id": lock_id,
                "memories": [],
            }

        ids = [d["memory_id"] for d in docs]

        result = await self.db.memory_kernel_ledger.update_many(
            {
                "memory_id": {"$in": ids},
                "provenance": Provenance.VE.value,
                "training_lock": {"$exists": False},
            },
            {
                "$set": {
                    "training_lock": lock_id,
                    "locked_at": utc_now(),
                }
            },
        )

        locked = await self.db.memory_kernel_ledger.find({
            "training_lock": lock_id,
            "provenance": Provenance.VE.value,
        }).to_list(length=limit)

        return {
            "ok": result.modified_count >= min_samples,
            "lock_id": lock_id,
            "count": len(locked),
            "memories": locked,
        }

    async def confirm_training_complete(
        self,
        *,
        memory_ids: List[str],
        lock_id: str,
    ) -> Dict[str, Any]:
        # Axiom — never weaken this guard.
        memories = await self.db.memory_kernel_ledger.find({
            "memory_id": {"$in": memory_ids}
        }).to_list(length=len(memory_ids))

        for memory in memories:
            if memory.get("provenance") != Provenance.VE.value:
                raise RuntimeError("Refusing to train on non-verified memory")

        result = await self.db.memory_kernel_ledger.update_many(
            {
                "memory_id": {"$in": memory_ids},
                "training_lock": lock_id,
                "provenance": Provenance.VE.value,
            },
            {
                "$set": {
                    "used_in_training": True,
                    "used_in_training_at": utc_now(),
                    "updated_at": utc_now(),
                },
                "$unset": {
                    "training_lock": "",
                    "locked_at": "",
                },
            },
        )

        return {
            "ok": True,
            "modified_count": result.modified_count,
            "lock_id": lock_id,
        }


class KernelGate:
    """
    MC routing gate.

    No organ / stack should route memory to execution / training directly.
    Every cross-component memory hop goes through `KernelGate.route()` so
    the route ledger is the single source of truth for "who saw what".
    """

    def __init__(self, db):
        self.db = db
        self.ledger = MemoryKernelLedger(db)

    async def route(
        self,
        *,
        memory_id: str,
        from_component: str,
        to_component: str,
    ) -> Dict[str, Any]:
        memory = await self.db.memory_kernel_ledger.find_one({"memory_id": memory_id})

        if not memory:
            allowed = False
            reason = "unknown_memory"
            provenance = Provenance.UV.value
        else:
            provenance = memory.get("provenance", Provenance.UV.value)

            if to_component == "training_pipeline":
                allowed = provenance == Provenance.VE.value
                reason = "ve_training_allowed" if allowed else "non_ve_training_blocked"

            elif to_component == "execution_engine":
                allowed = provenance == Provenance.VE.value
                reason = "ve_execution_reference_allowed" if allowed else "non_ve_execution_blocked"

            elif to_component in {"adversarial_critic", "governance_reviewer", "diagnostics"}:
                allowed = True
                reason = "critic_or_diagnostic_access"

            else:
                allowed = False
                reason = "unknown_destination"

        route_doc = await self.ledger.append_route_decision(
            memory_id=memory_id,
            from_component=from_component,
            to_component=to_component,
            route_allowed=allowed,
            reason=reason,
        )

        if not allowed:
            await self.db.memory_kernel_quarantine.insert_one({
                "memory_id": memory_id,
                "from_component": from_component,
                "to_component": to_component,
                "reason": reason,
                "provenance": provenance,
                "alert_level": "CRITICAL" if to_component == "execution_engine" else "WARNING",
                "created_at": utc_now(),
            })

        return {
            "allowed": allowed,
            "reason": reason,
            "provenance": provenance,
            "route": route_doc,
        }
