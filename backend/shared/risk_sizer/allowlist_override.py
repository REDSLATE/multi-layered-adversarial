"""A-quality override for the crypto BUY allowlist — SHIPPED DISABLED.

Operator directive (2026-07-31): prepare, but do NOT enable, a
tightly-controlled exception path: "A-quality signal + trusted market
data + minimum liquidity/history + operator-enabled override". The
override must NEVER fire solely because the doctrine score is high —
new/thin pairs produce misleadingly strong scores from incomplete
history. When (if) enabled, an override remains subject to RoadGuard,
pair minimums, spread, exposure caps and normal position sizing —
those gates run in the sizer regardless of this module's verdict.

Policy doc: runtime_flags._id=crypto_buy_allowlist_override
Managed via GET/PUT /api/admin/universe/crypto-buy-allowlist/override.
Missing doc → the disabled defaults below. Fail-CLOSED everywhere:
any read error or missing datum → no override.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("risedual.allowlist_override")

OVERRIDE_FLAG_ID = "crypto_buy_allowlist_override"
DEFAULT_POLICY = {
    "enabled": False,                      # master gate — operator only
    "min_doctrine_quality": "A_QUALITY",   # only A-quality may qualify
    "min_doctrine_score": 0.85,
    "require_bars_on_file": True,          # research_status must show bars
    "max_spread_bps": 50.0,                # thin books disqualify
    "min_confidence": 0.70,
}
_CACHE_TTL_S = 30.0
_cache: dict = {"at": 0.0, "doc": None}


def invalidate_cache() -> None:
    _cache.update(at=0.0, doc=None)


async def get_override_policy() -> dict:
    now = time.monotonic()
    if _cache["doc"] is not None and now - _cache["at"] < _CACHE_TTL_S:
        return _cache["doc"]
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": OVERRIDE_FLAG_ID}, {"_id": 0}, max_time_ms=3000,
    )
    merged = {**DEFAULT_POLICY, **(doc or {})}
    _cache.update(at=now, doc=merged)
    return merged


def _doctrine_of(intent: dict) -> tuple:
    """(quality, score) from the intent's doctrine packet."""
    packet = intent.get("doctrine_packet") or {}
    base = packet.get("base_labels") or {}
    quality = base.get("quality") or (intent.get("doctrine") or {}).get("quality")
    score = base.get("score")
    if score is None:
        score = (intent.get("doctrine") or {}).get("score")
    return quality, score


async def override_applies(intent: dict) -> tuple[bool, dict]:
    """(applies, receipt). Every check must pass on REAL data; any
    missing datum fails that check. Disabled policy → never applies."""
    try:
        pol = await get_override_policy()
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": "policy_read_failed", "error": str(exc)[:120]}
    if not pol.get("enabled"):
        return False, {"reason": "override_disabled"}

    checks: dict = {}
    quality, score = _doctrine_of(intent)
    checks["doctrine_quality"] = quality == pol["min_doctrine_quality"]
    try:
        checks["doctrine_score"] = (
            score is not None and float(score) >= float(pol["min_doctrine_score"])
        )
    except (TypeError, ValueError):
        checks["doctrine_score"] = False

    evidence = intent.get("evidence") or {}
    if pol.get("require_bars_on_file"):
        checks["bars_on_file"] = evidence.get("research_status") not in (
            None, "no_bars_on_file",
        )
    spread = evidence.get("spread_bps")
    if spread is None:
        spread = (intent.get("enriched_snapshot") or {}).get("spread_bps")
    try:
        checks["spread"] = (
            spread is not None and float(spread) <= float(pol["max_spread_bps"])
        )
    except (TypeError, ValueError):
        checks["spread"] = False
    try:
        checks["confidence"] = (
            float(intent.get("confidence") or 0.0) >= float(pol["min_confidence"])
        )
    except (TypeError, ValueError):
        checks["confidence"] = False

    applies = all(checks.values())
    receipt = {"reason": "all_checks_passed" if applies else "checks_failed",
               "checks": checks, "policy": pol}
    if applies:
        logger.warning(
            "allowlist OVERRIDE applied symbol=%s quality=%s score=%s — "
            "still subject to RoadGuard / spread / caps / sizing",
            intent.get("symbol"), quality, score,
        )
    return applies, receipt
