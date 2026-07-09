"""Bucket analyzer — groups resolved learning experiences into feature
bands and computes per-bucket edge stats.

Doctrine (2026-07-09 iter-22, Stage 2, operator directive):

    Lesson proposals require SAMPLE SIZE and STATISTICAL FLOOR.
    Guardrails:
      * min_sample_size = 30 resolved experiences per bucket
      * Wilson lower bound on hit-rate (z=1.96 for 95% CI)
      * expected_bps threshold: > 0 for edge, < -10 for bleed

Bucketing dimensions (kept small; more can be added when data grows):
    * lane                — equity | crypto
    * action              — BUY | SELL
    * notional_source     — brain_legacy | brain_v3 | micro_default |
                            micro_probe_failed_quality | env_default
    * rvol_band           — features.rvol → {low, normal, high, extreme, unknown}
    * spread_band         — features.spread_bps → {tight, normal, wide, extreme, unknown}
    * doctrine_result     — doctrine.execution_judge.failed_checks →
                            {clean, marginal_3, some_failed, no_packet}

Bucket id is a deterministic 16-char hash of the ordered tuple so the
same combination always writes to the same doc — no dupes, no drift.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from shared.learning.live_loop import LEARNING_EXPERIENCES

logger = logging.getLogger("shared.learning.bucket_analyzer")

LEARNING_BUCKETS = "learning_buckets"

# Bands — deliberately coarse. Refinement comes AFTER we know which
# dimensions actually predict outcome. Starting fine-grained would
# fragment the sample size and never cross the 30-count threshold.
_RVOL_BANDS = (
    ("low",     lambda v: v is not None and v < 0.8),
    ("normal",  lambda v: v is not None and 0.8 <= v < 1.5),
    ("high",    lambda v: v is not None and 1.5 <= v < 3.0),
    ("extreme", lambda v: v is not None and v >= 3.0),
)

_SPREAD_BANDS = (
    ("tight",   lambda v: v is not None and v < 10),
    ("normal",  lambda v: v is not None and 10 <= v < 30),
    ("wide",    lambda v: v is not None and 30 <= v < 80),
    ("extreme", lambda v: v is not None and v >= 80),
)


def _band(value: Any, bands: tuple) -> str:
    """Return the first band label whose predicate matches `value`,
    or 'unknown' if none match."""
    for label, pred in bands:
        try:
            if pred(value):
                return label
        except (TypeError, ValueError):
            continue
    return "unknown"


def _doctrine_band(doctrine: Optional[dict]) -> str:
    """Classify the doctrine packet's execution_judge state:
        clean         — no failed_checks
        marginal_3    — exactly the {liquidity_ok, quality_ok, score_ok} triple
        some_failed   — any other non-empty failed_checks
        no_packet     — doctrine_packet or execution_judge missing
    """
    if not isinstance(doctrine, dict):
        return "no_packet"
    seats = (doctrine.get("seats") or {}) if isinstance(doctrine, dict) else {}
    ej = seats.get("execution_judge") or {}
    if not ej:
        return "no_packet"
    failed = set(ej.get("failed_checks") or [])
    if not failed:
        return "clean"
    if failed == {"liquidity_ok", "quality_ok", "score_ok"}:
        return "marginal_3"
    return "some_failed"


def _bucket_key(*, lane, action, notional_source, rvol_band,
                spread_band, doctrine_band) -> tuple[str, str]:
    """Return (bucket_id, human_label). bucket_id is a stable 16-char
    hash — safe as a Mongo _id. Label is the human-readable tuple for
    dashboards."""
    tup = (
        (lane or "").lower(),
        (action or "").upper(),
        notional_source or "unknown",
        rvol_band, spread_band, doctrine_band,
    )
    label = "|".join(tup)
    bid = hashlib.sha256(label.encode()).hexdigest()[:16]
    return bid, label


def _extract_bucket_from_experience(exp: dict) -> tuple[str, str, dict]:
    """Extract bucket_id, label, and the raw dimensions dict for
    a single experience row."""
    features = exp.get("features") or {}
    rvol = features.get("rvol") or features.get("rvol_1m")
    spread = features.get("spread_bps") or features.get("spread")
    dims = {
        "lane": (exp.get("lane") or "").lower(),
        "action": (exp.get("action") or "").upper(),
        "notional_source": exp.get("notional_source") or "unknown",
        "rvol_band": _band(rvol, _RVOL_BANDS),
        "spread_band": _band(spread, _SPREAD_BANDS),
        "doctrine_band": _doctrine_band(exp.get("doctrine")),
    }
    bid, label = _bucket_key(**dims)
    return bid, label, dims


def wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float:
    """One-sided Wilson score lower bound on a binomial hit-rate.
    z=1.96 → 95% CI lower. Returns 0.0 for empty samples.

    Reference formula (Wilson 1927):
        (p̂ + z²/(2n) - z·sqrt(p̂(1-p̂)/n + z²/(4n²))) / (1 + z²/n)
    """
    if total <= 0:
        return 0.0
    p = wins / total
    denom = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total))
    return max(0.0, (centre - spread) / denom)


async def rebuild_buckets(db, *, only_resolved: bool = True) -> dict:
    """Walk resolved `learning_experiences` rows, aggregate into
    `learning_buckets` with sample count + avg_bps + hit_rate +
    wilson_lower. Full rebuild (upsert-per-bucket) so a single call
    always yields a fresh snapshot.

    Best-effort: never raises. Returns a counts dict.
    """
    counts: dict[str, Any] = {
        "experiences_scanned": 0, "buckets_written": 0, "errors": 0,
    }
    now_iso = datetime.now(timezone.utc).isoformat()

    # Aggregate in-process — the bucket count is small (bounded by
    # the cartesian product of band cardinalities, currently 2*2*5*5*5*4
    # = 2,000 possible buckets), so a dict fits comfortably.
    agg: dict[str, dict] = {}
    q: dict = {}
    if only_resolved:
        q["outcome_5m_bps"] = {"$ne": None}

    try:
        cur = db[LEARNING_EXPERIENCES].find(
            q,
            {
                "_id": 0, "lane": 1, "action": 1, "notional_source": 1,
                "features": 1, "doctrine": 1,
                "outcome_5m_bps": 1, "outcome_15m_bps": 1,
                "outcome_1h_bps": 1, "win": 1,
                "terminal_state": 1, "reject_reason": 1,
            },
        )
        async for exp in cur:
            counts["experiences_scanned"] += 1
            bid, label, dims = _extract_bucket_from_experience(exp)
            row = agg.setdefault(bid, {
                "_id": bid, "label": label, "dims": dims,
                "samples": 0, "wins": 0, "losses": 0, "sum_5m_bps": 0.0,
                "sum_15m_bps": 0.0, "sum_1h_bps": 0.0,
                "n_15m": 0, "n_1h": 0,
                "n_rejected": 0,
            })
            row["samples"] += 1
            bps5 = exp.get("outcome_5m_bps")
            if bps5 is not None:
                row["sum_5m_bps"] += bps5
                if exp.get("win") is True:
                    row["wins"] += 1
                elif exp.get("win") is False:
                    row["losses"] += 1
            bps15 = exp.get("outcome_15m_bps")
            if bps15 is not None:
                row["sum_15m_bps"] += bps15
                row["n_15m"] += 1
            bps1h = exp.get("outcome_1h_bps")
            if bps1h is not None:
                row["sum_1h_bps"] += bps1h
                row["n_1h"] += 1
            if exp.get("terminal_state") == "broker_rejected":
                row["n_rejected"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("bucket_analyzer: scan failed: %s", exc)
        return counts

    for bid, row in agg.items():
        n = row["samples"]
        won = row["wins"]
        row["avg_5m_bps"] = row["sum_5m_bps"] / n if n else 0.0
        row["avg_15m_bps"] = (
            row["sum_15m_bps"] / row["n_15m"] if row["n_15m"] else None
        )
        row["avg_1h_bps"] = (
            row["sum_1h_bps"] / row["n_1h"] if row["n_1h"] else None
        )
        # Hit-rate is over resolved-5m rows only; excludes rows still
        # pending resolution.
        resolved = won + row["losses"]
        row["hit_rate"] = won / resolved if resolved else None
        row["wilson_lower"] = wilson_lower_bound(won, resolved)
        row["updated_at"] = now_iso
        try:
            await db[LEARNING_BUCKETS].update_one(
                {"_id": bid}, {"$set": row}, upsert=True,
            )
            counts["buckets_written"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bucket_analyzer: bucket write failed bid=%s: %s", bid, exc,
            )
            counts["errors"] += 1

    if counts["experiences_scanned"]:
        logger.info(
            "bucket_analyzer: scanned=%d buckets=%d errors=%d",
            counts["experiences_scanned"], counts["buckets_written"],
            counts["errors"],
        )
    return counts
