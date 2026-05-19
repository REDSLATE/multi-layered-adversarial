"""Snapshot contract — MC's canonical field shape for intent enrichment.

Doctrine:
    Brains POST `/api/ingest/intent` with a `snapshot` block. MC's
    labelers read fields from that snapshot. Field NAMES are doctrine
    — `spread` ≠ `spread_bps`, `volume` ≠ `volume_24h_usd`. Drift
    silently produces sentinel-driven REJECTs.

    This module is the SINGLE SOURCE OF TRUTH for the snapshot
    contract. The labelers in `shared/doctrine/base_labels.py` and
    `shared/crypto/doctrine/crypto_labels.py` should be considered
    consumers of these constants; brains should be considered
    consumers of the same constants (fetched via
    `GET /api/runtime/snapshot-contract` at boot).

    A CI test (`tests/test_snapshot_contract.py`) locks the
    `contract_hash()` value. If MC updates this module, the hash
    changes, the test fails, and the operator must explicitly bump
    the known hash AND notify every brain to re-sync.

Tiers:
    MINIMUM: 7 canonical fields — sourced from Alpha's
        `services/intent_enrichment.py:SNAPSHOT_KEYS` so MC and Alpha
        agree byte-for-byte. These are the LOAD-BEARING fields whose
        absence triggers sentinel-driven blocks (e.g.,
        `spread_bps → 9999 → BLOCK_WIDE_SPREAD`).

    FULL_CRYPTO: 11 fields — adds 4 crypto-specific score-bonus
        fields (`funding_rate`, `open_interest_change_pct`,
        `liquidation_imbalance`, `btc_regime_alignment`). Missing
        these does NOT trigger blocks — they just leave score on the
        table.

    FULL_EQUITY: 8 fields — current MC equity labeler shape
        (gap-and-go doctrine). Distinct from FULL_CRYPTO; do NOT
        unify the two lanes.

Doctrine pin (2026-02-19):
    Minimum-tier completeness = first-fill readiness.
    Full-tier completeness = score-optimization phase.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


# ─── MINIMUM — the canonical 7-field contract ─────────────────────────
#
# Mirror of Alpha's `services/intent_enrichment.py:SNAPSHOT_KEYS`.
# These seven names are doctrine. Do not edit without bumping
# `CONTRACT_KNOWN_HASH` in tests/test_snapshot_contract.py AND
# notifying every brain agent.

SNAPSHOT_KEYS_MINIMUM: tuple[str, ...] = (
    "bid",
    "ask",
    "spread_bps",
    "volume_24h_usd",
    "volatility_1h",
    "trend_strength",
    "exchange_liquidity_score",
)

# Sentinel used when spread cannot be computed. Brains and MC must
# agree on this value — it's the "unknown" signal. 9999 bps = 99.99%
# spread, doctrinally impossible, so any code path that sees it knows
# the input is missing rather than catastrophic.
SPREAD_BPS_UNKNOWN: float = 9999.0


# ─── FULL_CRYPTO — extended fields for crypto score bonuses ───────────
#
# Pinned to `shared/crypto/doctrine/crypto_labels.py:label_crypto_snapshot`.
# Includes MINIMUM + 4 derivatives-specific fields that contribute
# additive score bonuses but never trigger blocks when missing.

SNAPSHOT_KEYS_FULL_CRYPTO: tuple[str, ...] = SNAPSHOT_KEYS_MINIMUM + (
    "funding_rate",
    "open_interest_change_pct",
    "liquidation_imbalance",
    "btc_regime_alignment",
)


# ─── FULL_EQUITY — gap-and-go doctrine fields ─────────────────────────
#
# Pinned to `shared/doctrine/base_labels.py:build_doctrine_labels`.
# Note: only `bid`, `ask`, `spread_bps`, `volume_24h_usd` overlap with
# MINIMUM — equity doctrine is gap/RVOL/float-driven, distinct
# vocabulary from the momentum-composite of MINIMUM.

SNAPSHOT_KEYS_FULL_EQUITY: tuple[str, ...] = (
    # Overlap with MINIMUM:
    "bid",
    "ask",
    "spread_bps",
    "volume_24h_usd",
    # Equity-specific:
    "price",
    "gap_pct",
    "relative_volume",
    "has_news",
    "float_millions",
    "pattern",
    "market_regime",
)


# ─── helpers ──────────────────────────────────────────────────────────


def compute_spread_bps(bid: Any, ask: Any) -> float:
    """Canonical spread formula. Returns `SPREAD_BPS_UNKNOWN` for any
    input the math can't trust — never raises, never returns NaN.

    Mirror of Alpha's `intent_enrichment.compute_spread_bps`. Identical
    semantics so MC's verification matches the brain's emission.
    """
    try:
        b = float(bid)
        a = float(ask)
    except (TypeError, ValueError):
        return SPREAD_BPS_UNKNOWN
    if b <= 0 or a <= 0 or not math.isfinite(b) or not math.isfinite(a):
        return SPREAD_BPS_UNKNOWN
    mid = (b + a) / 2.0
    if mid <= 0:
        return SPREAD_BPS_UNKNOWN
    return round(((a - b) / mid) * 10_000.0, 2)


def contract_hash() -> str:
    """sha256 of the canonical contract. Any brain shipping a contract
    with a different hash is out of sync with MC and should redeploy.

    Hash inputs (sorted-keys JSON):
        - minimum_keys
        - full_crypto_keys
        - full_equity_keys
        - spread_bps_unknown
    """
    payload = {
        "minimum_keys": list(SNAPSHOT_KEYS_MINIMUM),
        "full_crypto_keys": list(SNAPSHOT_KEYS_FULL_CRYPTO),
        "full_equity_keys": list(SNAPSHOT_KEYS_FULL_EQUITY),
        "spread_bps_unknown": SPREAD_BPS_UNKNOWN,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def contract_payload() -> dict:
    """Operator-readable contract document. Same payload the public
    HTTP endpoint returns. Brains hit this at boot to confirm they're
    using the right field names."""
    return {
        "contract_hash": contract_hash(),
        "spread_bps_unknown": SPREAD_BPS_UNKNOWN,
        "minimum_keys": list(SNAPSHOT_KEYS_MINIMUM),
        "full_crypto_keys": list(SNAPSHOT_KEYS_FULL_CRYPTO),
        "full_equity_keys": list(SNAPSHOT_KEYS_FULL_EQUITY),
        "doctrine": (
            "minimum-tier completeness unblocks first fill; "
            "full-tier completeness is score optimization. "
            "Field names are byte-exact — `spread` ≠ `spread_bps`. "
            "Missing fields default to sentinels that poison labeler output."
        ),
    }


__all__ = [
    "SNAPSHOT_KEYS_MINIMUM",
    "SNAPSHOT_KEYS_FULL_CRYPTO",
    "SNAPSHOT_KEYS_FULL_EQUITY",
    "SPREAD_BPS_UNKNOWN",
    "compute_spread_bps",
    "contract_hash",
    "contract_payload",
]
