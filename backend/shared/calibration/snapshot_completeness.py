"""Snapshot-completeness diagnostic — read-only.

Doctrine:
    Brains are supposed to POST `/api/ingest/intent` with a populated
    `snapshot` block. MC's doctrine pipeline reads field-by-field from
    that snapshot to compute spread/quality/score/execution_readiness.
    A missing field doesn't just lose a label — it defaults to a
    sentinel (e.g. `spread_bps → 9999`) that ACTIVELY POISONS the
    output (triggers `WIDE_SPREAD`, drops score, blocks execution).

    This endpoint reports — per lane, per brain — what fraction of
    directional intents are missing each required snapshot field.
    Operators see exactly which brain is silent on which input.

    OBSERVATION ONLY. Does not reject anything. The 422-on-ingest
    enforcement comes LATER, after brains have caught up.

Required field sets (pinned to the labelers):
    Crypto (`shared/crypto/doctrine/crypto_labels.py`):
        spread_bps, volume_24h_usd, volatility_1h, trend_strength,
        exchange_liquidity_score, funding_rate,
        open_interest_change_pct, liquidation_imbalance,
        btc_regime_alignment

    Equity (`shared/doctrine/base_labels.py`):
        price, gap_pct, relative_volume, has_news, float_millions,
        pattern, market_regime, spread_bps

    Both lanes: bid, ask are surfaced separately as "execution-grade
    fields" — required so spread can be re-verified at fill time and
    so the operator can audit slippage.

Endpoint:
    GET /api/admin/intents/snapshot-completeness
        ?lane=crypto|equity        (optional; default: both)
        ?hours=168                 (optional; default 7d)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.calibration.snapshot_contract import (
    SNAPSHOT_KEYS_FULL_CRYPTO,
    SNAPSHOT_KEYS_FULL_EQUITY,
    SNAPSHOT_KEYS_MINIMUM,
    contract_hash,
)


router = APIRouter(prefix="/admin/intents", tags=["intent-diagnostics"])


# ────────────────────── doctrine field sets ───────────────────────────


DIRECTIONAL_ACTIONS: frozenset[str] = frozenset({"BUY", "SELL", "SHORT", "COVER"})


# Re-exported for back-compat with existing tests. The single source of
# truth is now `shared/calibration/snapshot_contract.py`.
CRYPTO_REQUIRED_FIELDS: tuple[str, ...] = SNAPSHOT_KEYS_FULL_CRYPTO
EQUITY_REQUIRED_FIELDS: tuple[str, ...] = SNAPSHOT_KEYS_FULL_EQUITY

# Execution-grade fields — bid/ask are in MINIMUM but surfaced here too
# for the operator who's reading the diagnostic for "can the executor
# re-verify spread at fill time."
EXECUTION_GRADE_FIELDS: tuple[str, ...] = ("bid", "ask")


def _required_fields_for_lane(lane: Optional[str]) -> List[str]:
    """Return the union of fields a lane must populate. None / 'all'
    returns BOTH sets so the operator sees the full surface."""
    if lane == "crypto":
        return list(SNAPSHOT_KEYS_FULL_CRYPTO)
    if lane == "equity":
        return list(SNAPSHOT_KEYS_FULL_EQUITY)
    return sorted(set(SNAPSHOT_KEYS_FULL_CRYPTO) | set(SNAPSHOT_KEYS_FULL_EQUITY))


# ────────────────────── helpers ───────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _direction(intent: Dict[str, Any]) -> str:
    return str(intent.get("action") or intent.get("direction") or "").upper()


def _field_present(snapshot: Optional[Dict[str, Any]], field: str) -> bool:
    """A field is present iff snapshot[field] exists AND is non-null.

    Note: we accept zero-valued fields as present — a brain that
    correctly reports `spread_bps=0.0` (impossibly tight but a valid
    statement) is NOT the same as a brain that omits the field
    entirely and lets the default `9999.0` poison the doctrine.
    """
    if not isinstance(snapshot, dict):
        return False
    return field in snapshot and snapshot[field] is not None


# ────────────────────── core computation ──────────────────────────────


def _field_presence_block(
    rows: List[Dict[str, Any]],
    fields: List[str],
) -> Dict[str, Dict[str, Any]]:
    """For each field, count presence/missing across the row set."""
    out: Dict[str, Dict[str, Any]] = {}
    n = len(rows)
    for f in fields:
        present = sum(1 for r in rows if _field_present(r.get("snapshot"), f))
        out[f] = {
            "present": present,
            "missing": n - present,
            "presence_rate": round(present / n, 4) if n > 0 else 0.0,
        }
    return out


def _row_completeness_score(snapshot: Optional[Dict[str, Any]], fields: List[str]) -> float:
    """Fraction of required fields present on a single row. Used to
    histogram brains by "how complete is each intent on average"."""
    if not fields:
        return 1.0
    present = sum(1 for f in fields if _field_present(snapshot, f))
    return present / len(fields)


# ────────────────────── endpoint ──────────────────────────────────────


@router.get("/snapshot-completeness")
async def snapshot_completeness(
    lane: Optional[str] = Query(default=None, pattern="^(crypto|equity)$"),
    hours: int = Query(default=168, ge=1, le=24 * 90),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Read-only report: which snapshot fields each brain is missing.

    Operators read this to know what to ask of each brain's agent.
    """
    window_end = _now()
    window_start = window_end - timedelta(hours=hours)

    q: Dict[str, Any] = {
        "ingest_ts": {"$gte": window_start.isoformat()},
        "action": {"$in": list(DIRECTIONAL_ACTIONS)},
    }
    if lane:
        q["lane"] = lane

    projection = {
        "_id": 0,
        "stack": 1,
        "lane": 1,
        "symbol": 1,
        "action": 1,
        "snapshot": 1,
        "ingest_ts": 1,
    }
    rows: List[Dict[str, Any]] = await db[SHARED_INTENTS].find(q, projection).to_list(50000)
    total = len(rows)

    required = _required_fields_for_lane(lane)
    aggregate_presence = _field_presence_block(rows, required)

    # Per-tier presence — what the operator needs to know first.
    # MINIMUM tier going green = first-fill readiness.
    # FULL tier going green = score-optimization phase.
    minimum_keys = list(SNAPSHOT_KEYS_MINIMUM)

    def _tier_completeness(subset_rows: List[Dict[str, Any]], keys: List[str]) -> Dict[str, Any]:
        """Per-row completeness aggregated across a tier."""
        if not subset_rows:
            return {"intents": 0, "average_completeness": 0.0, "fully_complete": 0}
        completeness_scores = [
            _row_completeness_score(r.get("snapshot"), keys) for r in subset_rows
        ]
        fully_complete = sum(1 for s in completeness_scores if s >= 0.9999)
        return {
            "intents": len(subset_rows),
            "average_completeness": round(sum(completeness_scores) / len(completeness_scores), 4),
            "fully_complete": fully_complete,
        }

    # Per-brain breakdown — tiered: minimum + lane-specific full.
    by_brain: Dict[str, Dict[str, Any]] = {}
    brain_ids = sorted({str(r.get("stack") or "").lower() for r in rows if r.get("stack")})
    for stack in brain_ids:
        subset = [r for r in rows if str(r.get("stack") or "").lower() == stack]
        # Brain's intents may span both lanes; bucket by lane for full-tier
        crypto_subset = [r for r in subset if str(r.get("lane") or "").lower() == "crypto"]
        equity_subset = [r for r in subset if str(r.get("lane") or "").lower() == "equity"]
        avg_completeness = (
            sum(_row_completeness_score(r.get("snapshot"), required) for r in subset) / len(subset)
            if subset
            else 0.0
        )
        by_brain[stack] = {
            "total_directional_intents": len(subset),
            "average_completeness": round(avg_completeness, 4),
            "field_presence": _field_presence_block(subset, required),
            "tiers": {
                "minimum": _tier_completeness(subset, minimum_keys),
                "full_crypto": _tier_completeness(crypto_subset, list(SNAPSHOT_KEYS_FULL_CRYPTO)),
                "full_equity": _tier_completeness(equity_subset, list(SNAPSHOT_KEYS_FULL_EQUITY)),
            },
        }

    # Per-lane breakdown when no lane filter (so the operator sees both
    # in one call). When lane is set, this just mirrors the aggregate.
    by_lane: Dict[str, Dict[str, Any]] = {}
    if lane is None:
        for lane_name, lane_fields in (
            ("crypto", list(CRYPTO_REQUIRED_FIELDS) + list(EXECUTION_GRADE_FIELDS)),
            ("equity", list(EQUITY_REQUIRED_FIELDS) + list(EXECUTION_GRADE_FIELDS)),
        ):
            subset = [r for r in rows if str(r.get("lane") or "").lower() == lane_name]
            by_lane[lane_name] = {
                "total_directional_intents": len(subset),
                "field_presence": _field_presence_block(subset, lane_fields),
            }

    # Worst-offender summary: which (brain, field) pairs are 0% present?
    # Operators want a one-glance list of "ask these brains for these
    # fields first."
    worst_offenders: List[Dict[str, Any]] = []
    for stack, brain_block in by_brain.items():
        for f, presence in brain_block["field_presence"].items():
            if presence["presence_rate"] == 0.0 and presence["missing"] > 0:
                worst_offenders.append({
                    "brain": stack,
                    "field": f,
                    "missing": presence["missing"],
                })
    worst_offenders.sort(key=lambda x: (-x["missing"], x["brain"], x["field"]))

    return {
        "lane": lane or "all",
        "hours": hours,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "directional_actions": sorted(DIRECTIONAL_ACTIONS),
        "total_directional_intents": total,
        "fields_required_for_doctrine": required,
        "snapshot_contract_hash": contract_hash(),
        "tier_keys": {
            "minimum": list(SNAPSHOT_KEYS_MINIMUM),
            "full_crypto": list(SNAPSHOT_KEYS_FULL_CRYPTO),
            "full_equity": list(SNAPSHOT_KEYS_FULL_EQUITY),
        },
        "crypto_required_fields": list(CRYPTO_REQUIRED_FIELDS),
        "equity_required_fields": list(EQUITY_REQUIRED_FIELDS),
        "execution_grade_fields": list(EXECUTION_GRADE_FIELDS),
        "field_presence": aggregate_presence,
        "by_brain": by_brain,
        "by_lane": by_lane,
        "worst_offenders": worst_offenders[:20],
        "notes": [
            "TIERED COMPLETENESS: `minimum` tier going green per brain = "
            "first-fill readiness (the 7 load-bearing fields whose absence "
            "triggers sentinel blocks). `full_crypto` / `full_equity` tier "
            "going green = score-optimization phase (additional bonus fields "
            "that don't poison output when missing).",
            "Snapshot fields are read by `shared/crypto/doctrine/crypto_labels.py` "
            "(crypto) and `shared/doctrine/base_labels.py` (equity). Missing "
            "MINIMUM fields default to sentinels that POISON the doctrine "
            "output: `spread_bps → 9999.0` triggers BLOCK_WIDE_SPREAD; zero "
            "scoring fields hold `base.score` at 0.00 (REJECT) which keeps "
            "`execution_judge.execution_ready=false`. The pipeline is correct; "
            "the inputs are empty.",
            "Single source of truth for the contract is "
            "`shared/calibration/snapshot_contract.py`. Brains should fetch "
            "the contract at boot via `GET /api/runtime/snapshot-contract` "
            "(no auth required, doctrine-pinned read). The `snapshot_contract_hash` "
            "field above lets brains CI-test their local copy against MC.",
            "Zero-valued fields (e.g. `spread_bps=0.0`) count as PRESENT — a "
            "brain explicitly reporting a value is not the same as omitting "
            "the field.",
            "HOLD and non-directional actions are EXCLUDED. Snapshot "
            "completeness on a HOLD doesn't matter (HOLD never reaches the "
            "executor anyway).",
            "This endpoint is READ-ONLY OBSERVATION. The strict-validation "
            "422-on-ingest path is a future change — only after brains have "
            "caught up to populating the MINIMUM tier.",
        ],
    }
