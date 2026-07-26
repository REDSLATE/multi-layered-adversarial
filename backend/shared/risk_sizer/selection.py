"""Primary-brain selection scoring — lives INSIDE the MC arbiter's
winner path (no second authority layer).

Eligibility: directional action, confidence floor, and — only once a
brain has ≥20 resolved trades in the lane — positive verified
expectancy. Below the sample threshold expectancy must not hard-block
and must not act as a hidden penalty: the remaining weights are
renormalized to 1.0 (0.35/0.30/0.20 → /0.85).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("risedual.risk_sizer.selection")

_EXP_TTL_S = 300.0
_exp_cache: dict[str, dict[str, Any]] = {}   # lane → {at, by_brain}


def reset_for_tests() -> None:
    _exp_cache.clear()


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


async def _brain_expectancy(lane: str) -> dict[str, dict]:
    """{brain: {trades, expectancy_usd}} over the last 30 days of
    resolved outcomes. Cached 5 min — arbiter runs pulse-side, but a
    full scan per pulse is still waste."""
    cached = _exp_cache.get(lane)
    if cached and time.monotonic() - cached["at"] < _EXP_TTL_S:
        return cached["by_brain"]
    by_brain: dict[str, dict] = {}
    try:
        from datetime import datetime, timedelta, timezone
        from db import db  # noqa: WPS433
        from shared.expectancy import EXIT_OUTCOMES, get_config, row_costs  # noqa: WPS433
        cfg = await get_config()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cursor = db[EXIT_OUTCOMES].find(
            {"lane": lane, "closed_at": {"$gte": cutoff}}, {"_id": 0},
        )
        async for row in cursor:
            c = row_costs(row, cfg)
            if c is None:
                continue
            b = by_brain.setdefault(row.get("brain") or "unattributed",
                                    {"trades": 0, "net": 0.0})
            b["trades"] += 1
            b["net"] += c["net"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain expectancy load failed lane=%s: %s", lane, exc)
    out = {
        brain: {
            "trades": v["trades"],
            "expectancy_usd": (v["net"] / v["trades"]) if v["trades"] else 0.0,
        }
        for brain, v in by_brain.items()
    }
    _exp_cache[lane] = {"at": time.monotonic(), "by_brain": out}
    return out


async def annotate_candidates(candidates: list[dict], lane: str) -> list[dict]:
    """Mutates each ranked candidate with `eligible`,
    `selection_score`, `selection_detail`."""
    from shared.risk_sizer.policy import get_sizer_policy  # noqa: WPS433
    sel_pol = (await get_sizer_policy())["selection"]
    min_conf = float(sel_pol["min_confidence"])
    min_score = float(sel_pol["min_score"])
    min_sample = int(sel_pol["min_expectancy_sample"])
    expectancy = await _brain_expectancy(lane)

    for cand in candidates:
        op = cand.get("opinion") or {}
        conf = _clamp01(float(op.get("confidence") or 0.0))
        regime = op.get("regime_match")
        regime = _clamp01(float(regime)) if regime is not None else 0.5
        detail: dict[str, Any] = {"confidence": conf, "regime_match": regime}

        try:
            from shared.brains.kernel_throttle import get_kernel_throttle  # noqa: WPS433
            k = await get_kernel_throttle(cand["brain"], lane)
            kernel = _clamp01(float(k.get("score"))) if k.get("score") is not None else 0.5
        except Exception:  # noqa: BLE001
            kernel = 0.5
        detail["kernel_score"] = kernel

        exp = expectancy.get(cand["brain"]) or {"trades": 0, "expectancy_usd": 0.0}
        detail["resolved_trades"] = exp["trades"]
        detail["expectancy_usd"] = round(exp["expectancy_usd"], 4)

        eligible = True
        reason = None
        if conf < min_conf:
            eligible, reason = False, f"confidence<{min_conf}"
        elif exp["trades"] >= min_sample and exp["expectancy_usd"] <= 0:
            eligible, reason = False, "negative_verified_expectancy"

        if exp["trades"] >= min_sample:
            exp_score = _clamp01(min(exp["expectancy_usd"], 1.0))
            score = (conf * 0.35 + regime * 0.30 + kernel * 0.20
                     + exp_score * 0.15)
            detail["expectancy_weighted"] = True
        else:
            # Renormalize — no artificial 15% penalty pre-sample.
            score = (conf * (0.35 / 0.85) + regime * (0.30 / 0.85)
                     + kernel * (0.20 / 0.85))
            detail["expectancy_weighted"] = False

        if eligible and score < min_score:
            eligible, reason = False, f"score<{min_score}"

        cand["eligible"] = eligible
        cand["selection_score"] = round(score, 4)
        cand["ineligible_reason"] = reason
        cand["selection_detail"] = detail
    return candidates
