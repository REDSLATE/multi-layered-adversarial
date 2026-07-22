"""Opportunity policy — aggression knobs, NOT the safety layer.

2026-07-22 operator doctrine: "To make it trade more aggressively,
change the opportunity policy, not the execution safety layer."

    Positive but uncertain → trade smaller   (PROBE)
    Strong and confirmed   → trade larger    (full size)
    Ordinary disagreement  → reduce size     (arbiter, already live)
    Execution danger       → block           (risk gate, untouched)

Knobs (runtime_flags._id=opportunity_policy, ~15s cache):
  * authority_min   — per-lane intent execution-authority window
                      (minutes). Retention stays 72h; a stale signal
                      is RETAINED but never EXECUTED past this.
  * tiers           — per-lane conviction thresholds:
                      conf < probe            → WATCH (no capital)
                      probe ≤ conf < enter    → PROBE notional
                      enter ≤ conf < press    → ENTER notional
                      conf ≥ press            → FULL notional
  * tier_notionals  — USD per tier (risk per-order cap still clamps).
  * kernel          — Rise Kernel throttle (hot-score → size
                      multiplier, clamp [min_mult, max_mult]).
                      Throttle, never veto.
"""
from __future__ import annotations

import time
from typing import Any

from db import db

POLICY_FLAG_ID = "opportunity_policy"
_CACHE_TTL_S = 15.0

DEFAULTS: dict[str, Any] = {
    "tiers_enabled": True,
    "authority_min": {"equity": 15.0, "crypto": 30.0},
    "tiers": {
        "equity": {"probe": 0.32, "enter": 0.40, "press": 0.62},
        "crypto": {"probe": 0.32, "enter": 0.35, "press": 0.62},
    },
    "tier_notionals": {"probe": 5.0, "enter": 7.5, "full": 10.0},
    "kernel": {"enabled": True, "min_mult": 0.50, "max_mult": 1.35},
}

_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _merge(stored: dict) -> dict:
    out: dict = {"tiers_enabled": bool(
        stored.get("tiers_enabled", DEFAULTS["tiers_enabled"])
    )}
    am = stored.get("authority_min") or {}
    out["authority_min"] = {
        lane: float(am.get(lane, DEFAULTS["authority_min"][lane]))
        for lane in ("equity", "crypto")
    }
    tiers_in = stored.get("tiers") or {}
    out["tiers"] = {}
    for lane in ("equity", "crypto"):
        lt = tiers_in.get(lane) or {}
        out["tiers"][lane] = {
            k: float(lt.get(k, DEFAULTS["tiers"][lane][k]))
            for k in ("probe", "enter", "press")
        }
    tn = stored.get("tier_notionals") or {}
    out["tier_notionals"] = {
        k: float(tn.get(k, DEFAULTS["tier_notionals"][k]))
        for k in ("probe", "enter", "full")
    }
    kn = stored.get("kernel") or {}
    out["kernel"] = {
        "enabled": bool(kn.get("enabled", DEFAULTS["kernel"]["enabled"])),
        "min_mult": float(kn.get("min_mult", DEFAULTS["kernel"]["min_mult"])),
        "max_mult": float(kn.get("max_mult", DEFAULTS["kernel"]["max_mult"])),
    }
    return out


async def get_opportunity_policy() -> dict:
    now = time.monotonic()
    if _cache["value"] is not None and (now - _cache["at"]) < _CACHE_TTL_S:
        return _cache["value"]
    try:
        stored = await db["runtime_flags"].find_one(
            {"_id": POLICY_FLAG_ID}, {"_id": 0},
        ) or {}
    except Exception:  # noqa: BLE001
        stored = {}
    merged = _merge(stored)
    _cache.update(at=now, value=merged)
    return merged


def invalidate_policy_cache() -> None:
    _cache.update(at=0.0, value=None)


def classify_tier(confidence: float, lane: str, policy: dict) -> tuple[str, float]:
    """(tier, base_notional_usd). tier ∈ WATCH|PROBE|ENTER|FULL.
    WATCH → notional 0.0 (no capital)."""
    t = policy["tiers"].get(lane) or policy["tiers"]["equity"]
    n = policy["tier_notionals"]
    c = float(confidence or 0.0)
    if c < t["probe"]:
        return "WATCH", 0.0
    if c < t["enter"]:
        return "PROBE", n["probe"]
    if c < t["press"]:
        return "ENTER", n["enter"]
    return "FULL", n["full"]
