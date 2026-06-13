"""Paradox v2 idempotent seed.

Seeds the four canonical brains, the two execution seats (equity,
crypto), the equity_executor's conservative policy, and the foundational
trust list. Crypto executor starts in `observe` mode with NO trusted
brains — per the Paradox v2 doctrine reset, restrictions belong to the
SEAT, and the operator (or verifier autonomy progression) is the only
path to giving a brain trust on the crypto lane.

Safe to re-run. Every collection writer uses upsert keyed by the IP
boundary (brain_id, seat_id, or (seat_id, brain_id) for trust rows).
"""
from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from db import db
from namespaces import (
    PARADOX_V2_BRAIN_REGISTRY,
    PARADOX_V2_GOVERNOR_RULES,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Canonical brain doctrine map (brain_id → display + doctrine).
CANONICAL_BRAINS = [
    {"brain_id": "alpha",    "display_name": "Camino",    "doctrine": "adversarial"},
    {"brain_id": "camaro",   "display_name": "Barracuda", "doctrine": "tape_reading"},
    {"brain_id": "chevelle", "display_name": "Hellcat",   "doctrine": "trend"},
    {"brain_id": "redeye",   "display_name": "GTO",       "doctrine": "mean_reversion"},
]


# Default seat policies. Crypto starts observe-only, no trust.
DEFAULT_SEAT_POLICIES = [
    {
        "seat_id": "equity_executor",
        "autonomy_mode": "auto_execute",
        "enabled": True,
        "max_notional_usd": 5_000.0,
        "size_multiplier": 0.50,
        "daily_risk_budget_usd": 25_000.0,
        "max_position_count": 10,
        "max_concentration_pct": 25.0,
        "confidence_min": 0.85,
        "market_quality_min": 0.60,
        "max_auditor_objections": 0,
        "required_governor_stance": "RISK_DOWN",
    },
    {
        "seat_id": "crypto_executor",
        "autonomy_mode": "observe",     # paper-only until verifier promotes
        "enabled": True,
        "max_notional_usd": 1_000.0,
        "size_multiplier": 0.25,
        "daily_risk_budget_usd": 5_000.0,
        "max_position_count": 5,
        "max_concentration_pct": 15.0,
        "confidence_min": 0.90,         # higher bar to earn promotion
        "market_quality_min": 0.70,
        "max_auditor_objections": 0,
        "required_governor_stance": "RISK_DOWN",
    },
]


# Default trust: equity_executor trusts alpha at 1.0 (existing equity
# executor in the legacy roster). All other seat × brain pairs start
# untrusted. Operator adds via /api/v2/seat-trust.
DEFAULT_TRUST = [
    {"seat_id": "equity_executor", "brain_id": "alpha", "trust_level": 1.0},
]


# Default governor modifier rules. Each maps a market-quality signal
# to a structured size reduction. Never blocks; the SEAT does the
# blocking via its own policy gates.
DEFAULT_GOVERNOR_RULES = [
    {
        "rule_id": "wide_spread",
        "trigger_type": "wide_spread",
        "trigger_threshold": 6.7,
        "size_multiplier": 0.50,
        "vote_required": False,
        "increase_scrutiny": True,
        "flag_anomaly": False,
        "reason_template": "Wide spread detected ({spread_bps:.1f} bps). Reducing size 50%.",
        "is_active": True,
    },
    {
        "rule_id": "low_rvol",
        "trigger_type": "low_rvol",
        "trigger_threshold": 0.8,
        "size_multiplier": 0.50,
        "vote_required": False,
        "increase_scrutiny": False,
        "flag_anomaly": False,
        "reason_template": "Relative volume below floor ({rvol:.2f}x). Reducing size 50%.",
        "is_active": True,
    },
    {
        "rule_id": "earnings_window",
        "trigger_type": "earnings_window",
        "trigger_threshold": 1.0,  # boolean signal
        "size_multiplier": 0.25,
        "vote_required": True,
        "increase_scrutiny": True,
        "flag_anomaly": False,
        "reason_template": "Earnings within window. Reducing size 75% and escalating to vote.",
        "is_active": True,
    },
]


async def seed_paradox_v2() -> dict[str, Any]:
    """Idempotent seed. Returns count of upserts per collection.

    Safe to call multiple times on the same DB. Uses upsert keyed by
    each collection's natural primary key, so re-running never duplicates
    or overwrites operator-edited fields beyond the seed defaults.
    """
    counts: dict[str, int] = {}
    now = _now()

    # Brains
    n = 0
    for b in CANONICAL_BRAINS:
        r = await db[PARADOX_V2_BRAIN_REGISTRY].update_one(
            {"brain_id": b["brain_id"]},
            {
                "$setOnInsert": {
                    **b,
                    "version": "1.0.0",
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                },
            },
            upsert=True,
        )
        if r.upserted_id:
            n += 1
    counts["brain_registry"] = n

    # Seat policies
    n = 0
    for p in DEFAULT_SEAT_POLICIES:
        r = await db[PARADOX_V2_SEAT_POLICY].update_one(
            {"seat_id": p["seat_id"]},
            {
                "$setOnInsert": {
                    **p,
                    "updated_at": now,
                    "updated_by": "seed",
                },
            },
            upsert=True,
        )
        if r.upserted_id:
            n += 1
    counts["seat_policy_config"] = n

    # Trust list
    n = 0
    for t in DEFAULT_TRUST:
        r = await db[PARADOX_V2_SEAT_TRUSTED].update_one(
            {"seat_id": t["seat_id"], "brain_id": t["brain_id"]},
            {
                "$setOnInsert": {
                    **t,
                    "added_at": now,
                    "added_by": "seed",
                },
            },
            upsert=True,
        )
        if r.upserted_id:
            n += 1
    counts["seat_trusted_brains"] = n

    # Governor rules
    n = 0
    for g in DEFAULT_GOVERNOR_RULES:
        r = await db[PARADOX_V2_GOVERNOR_RULES].update_one(
            {"rule_id": g["rule_id"]},
            {
                "$setOnInsert": {**g, "created_at": now},
            },
            upsert=True,
        )
        if r.upserted_id:
            n += 1
    counts["governor_modifier_rules"] = n

    return {"ok": True, "seeded": counts, "ts": now}
