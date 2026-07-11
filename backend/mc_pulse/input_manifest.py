"""Input feature manifest — the honest record of WHAT each brain
saw when it evaluated, and WHAT it decided.

Operator directive (2026-02): before we can compare Camino's
runner and pulse decisions, we have to compare their INPUTS.
Rationale-token similarity, action distribution overlap, and
timestamp drift are all downstream of the input contract. If the
runner sees `spread_bps=3.0, trend_score=0.42, setup_score=0.71`
and the pulse sees `spread_bps=<absent>, trend_score=<absent>,
setup_score=<absent>`, the two "Caminos" aren't the same brain
running two engines — they're evaluating two entirely different
market descriptions and it's an accident when they ever agree.

The manifest is intentionally redacted (rounded to 4dp) so
bit-level float jitter across OS / Python builds doesn't produce
spurious digest mismatches. Its schema:

    parity_key           — canonical join key string
    path                 — "runner" | "pulse"
    evaluation_at        — brain's wall-clock emission (UTC ISO)
    source_bar_open_at   — from BarIdentity, NOT wall-clock
    source_bar_close_at  — from BarIdentity, NOT wall-clock
    source_bar_id        — feeder-provided uid when present
                           (fallback: str(open_at))
    source               — "shared_ohlcv_bars", etc.
    available_fields     — sorted list of feature names present
    missing_fields       — expected fields absent
    feature_values       — {name: rounded float}
    feature_digest       — sha256 of (available_fields, values)
                           NOT part of ParityKey — parity's whole
                           point is to compare these across paths
    fallback_used        — True when the canonical builder took
                           its cold-start branch (bars < 20). The
                           runner and pulse both fall back; a
                           fallback-vs-hot mismatch is itself a
                           parity finding.
    position_context_present — did we know an open position?
    bar_count            — how many bars fed the snapshot
    action               — brain's final action ("BUY"/"SELL"/"HOLD")
    confidence           — brain's final confidence [0.0, 1.0]
    status               — OpinionStatus (OK / INSUFFICIENT_DATA / ...)
    reason_codes         — tuple of structured reason codes

Persistence contract:
    Collection: `mc_parity_manifests`
    Unique index: (parity_key, path)  — the runner and pulse
        rows for the SAME key must coexist; a unique on just
        parity_key would let one path overwrite the other, which
        is precisely the data parity needs.
    TTL 7d — parity is short-window work.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from db import db
from mc_pulse.parity_key import BarIdentity, ParityKey

logger = logging.getLogger("mc_pulse.input_manifest")

MANIFEST_COLLECTION = "mc_parity_manifests"

# Fields the `NeutralAdversarialBrain._build_hypotheses` legacy
# core actually READS. If ANY of these are missing on the pulse
# snapshot, the core's `_clamp` falls back to defaults that pin
# the HOLD/OBSERVE hypotheses to 1.0 (via `spread_bps=9999` +
# `volatility=0` defaults) — the exact failure mode that produced
# the 100% HOLD @ confidence 1.0 signature.
#
# Camino specifically needs: trend_score, price_change_pct,
# volume_change_pct, rsi, spread_bps, volatility, liquidity_score,
# setup_score. Optional but doctrine-consumed:
# gap_pct, relative_volume, vwap_distance_pct.
CAMINO_REQUIRED_FIELDS = frozenset({
    "trend_score",
    "price_change_pct",
    "volume_change_pct",
    "rsi",
    "spread_bps",
    "volatility",
    "liquidity_score",
    "setup_score",
})

CAMINO_DOCTRINE_FIELDS = frozenset({
    "gap_pct",
    "relative_volume",
    "vwap_distance_pct",
    "market_regime",
    "spread_quality",
})


@dataclass(frozen=True, slots=True)
class InputManifest:
    """Redacted view of what the brain saw AND decided. Frozen
    because the manifest is an audit record — once written it
    MUST NOT change; downstream parity math relies on
    (parity_key, path) → manifest being stable."""
    parity_key: ParityKey
    path: str                                    # "runner" | "pulse"
    evaluation_at: str                           # ISO wall-clock
    bar: BarIdentity
    source_bar_id: str                           # feeder uid or str(open_at)
    available_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    feature_values: Mapping[str, float]
    fallback_used: bool                          # canonical builder cold-branch fired
    position_context_present: bool
    bar_count: int
    action: str                                  # "BUY" | "SELL" | "HOLD"
    confidence: float
    status: str                                  # OpinionStatus value
    reason_codes: tuple[str, ...] = ()
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    @property
    def feature_digest(self) -> str:
        """Stable sha256 of (available_fields, feature_values).

        NOT part of ParityKey. If runner and pulse manifests
        share the same digest for the same parity_key, they
        truly saw the same inputs and any downstream divergence
        is attributable to the core, not the feature layer.
        Mismatched digests are the finding parity is meant to
        produce — hiding them inside the join key would defeat
        the exercise.
        """
        payload = {
            "fields": sorted(self.available_fields),
            "values": {
                k: self.feature_values.get(k)
                for k in sorted(self.feature_values)
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()

    def to_mongo(self) -> dict:
        """Flat dict for the manifest collection. Uniqueness is
        (parity_key, path) — see `db.ensure_indexes`."""
        return {
            "parity_key": self.parity_key.as_string(),
            "brain_id": self.parity_key.brain_id,
            "symbol": self.parity_key.symbol,
            "timeframe": self.parity_key.timeframe,
            "source_bar_close_at": self.parity_key.source_bar_close_at,
            "snapshot_schema_version": self.parity_key.snapshot_schema_version,
            "path": self.path,
            "evaluation_at": self.evaluation_at,
            "source_bar_open_at": self.bar.open_at.isoformat(),
            "source": self.bar.source,
            "source_bar_id": self.source_bar_id,
            "available_fields": list(self.available_fields),
            "missing_fields": list(self.missing_fields),
            "feature_values": dict(self.feature_values),
            "feature_digest": self.feature_digest,
            "fallback_used": self.fallback_used,
            "position_context_present": self.position_context_present,
            "bar_count": self.bar_count,
            "action": self.action,
            "confidence": self.confidence,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "recorded_at": self.recorded_at,
        }


def _round_for_stability(v: Any) -> Optional[float]:
    """Round to 4 decimals so bit-level float jitter across
    OS / Python builds doesn't produce distinct feature_hashes
    for what is effectively the same value.

    None / non-numeric returns None so the manifest can distinguish
    "field present but unset" from "field absent."
    """
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def build_camino_manifest(
    *,
    parity_key: ParityKey,
    path: str,
    bar: BarIdentity,
    source_bar_id: str,
    snapshot: Mapping[str, Any],
    fallback_used: bool,
    position_context_present: bool,
    bar_count: int,
    action: str,
    confidence: float,
    status: str = "OK",
    reason_codes: Iterable[str] = (),
    evaluation_at: Optional[datetime | str] = None,
    required_fields: Iterable[str] = CAMINO_REQUIRED_FIELDS,
    doctrine_fields: Iterable[str] = CAMINO_DOCTRINE_FIELDS,
) -> InputManifest:
    """Build a Camino-shaped manifest from a canonical snapshot dict.

    Both runner and pulse call this — the SAME manifest shape
    from both sides is the entire point of the exercise.
    """
    required = set(required_fields)
    doctrine = set(doctrine_fields)
    watched = required | doctrine

    numeric_values: dict[str, float] = {}
    non_numeric_present: set[str] = set()
    for name in sorted(watched):
        if name not in snapshot or snapshot[name] is None:
            continue
        val = snapshot[name]
        if isinstance(val, (int, float)):
            rounded = _round_for_stability(val)
            if rounded is not None:
                numeric_values[name] = rounded
        else:
            non_numeric_present.add(name)

    available_fields = tuple(sorted(set(numeric_values) | non_numeric_present))
    missing_fields = tuple(sorted(watched - set(available_fields)))

    if isinstance(evaluation_at, datetime):
        eval_iso = evaluation_at.astimezone(timezone.utc).isoformat()
    elif isinstance(evaluation_at, str):
        eval_iso = evaluation_at
    else:
        eval_iso = datetime.now(timezone.utc).isoformat()

    return InputManifest(
        parity_key=parity_key,
        path=path,
        evaluation_at=eval_iso,
        bar=bar,
        source_bar_id=source_bar_id,
        available_fields=available_fields,
        missing_fields=missing_fields,
        feature_values=numeric_values,
        fallback_used=bool(fallback_used),
        position_context_present=bool(position_context_present),
        bar_count=int(bar_count or 0),
        action=(action or "").upper(),
        confidence=float(confidence or 0.0),
        status=status or "OK",
        reason_codes=tuple(reason_codes or ()),
    )


async def persist_manifest(manifest: InputManifest) -> None:
    """Upsert manifest by (parity_key, path). Bounded, fail-soft.

    Fail-soft is intentional: parity is an OBSERVABILITY feature;
    a wedged manifest write must never take down the runner or
    the pulse. A missing manifest simply means "we don't know if
    these two evaluations shared inputs" — downstream parity
    metrics will reflect that as `manifests_missing > 0`.
    """
    try:
        doc = manifest.to_mongo()
        await db[MANIFEST_COLLECTION].update_one(
            {"parity_key": doc["parity_key"], "path": doc["path"]},
            {"$set": doc, "$setOnInsert": {"first_recorded_at": doc["recorded_at"]}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "manifest upsert failed parity_key=%s path=%s err=%s",
            manifest.parity_key.hash(), manifest.path, exc,
        )
