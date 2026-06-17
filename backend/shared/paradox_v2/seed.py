"""Paradox v2 idempotent seed.

Seeds the four canonical brains, the four execution seats (two live —
equity_executor + crypto_executor; two pilot — spot_short_executor +
options_executor), the equity_executor's conservative policy, and the
foundational trust list. Crypto, spot_short, and options executors all
start in `observe` mode (pilot/decision-only). Per Paradox v2 doctrine
restrictions belong to the SEAT — the operator (or verifier autonomy
progression) is the only path to giving execution rights on a new lane.

Phase 3 pilot seats (2026-02-19):
  - spot_short_executor → camaro_ops (Barracuda is the doctrine fit for
    short-side tape reading).
  - options_executor    → chevelle_ops (Hellcat's trend doctrine maps
    to directional options selection).

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
    {"brain_id": "camino",    "display_name": "Camino",    "doctrine": "adversarial"},
    {"brain_id": "barracuda",   "display_name": "Barracuda", "doctrine": "tape_reading"},
    {"brain_id": "hellcat", "display_name": "Hellcat",   "doctrine": "trend"},
    {"brain_id": "gto",   "display_name": "GTO",       "doctrine": "mean_reversion"},
]


# Default seat policies. Crypto starts observe-only, no trust.
# Phase 3 pilot seats (spot_short, options) also start observe-only.
DEFAULT_SEAT_POLICIES = [
    {
        "seat_id": "equity_executor",
        "instrument_type": "equity_long",
        "autonomy_mode": "auto_execute",
        "enabled": True,
        "max_notional_usd": 5_000.0,
        "size_multiplier": 0.50,
        "daily_risk_budget_usd": 25_000.0,
        "max_position_count": 10,
        "max_concentration_pct": 25.0,
        "confidence_min": 0.85,
        "market_quality_min": 0.60,
        "required_governor_stance": "RISK_DOWN",
    },
    {
        "seat_id": "crypto_executor",
        "instrument_type": "crypto_spot",
        "autonomy_mode": "observe",     # decision-only until verifier promotes (NO execution; no paper trades exist)
        "enabled": True,
        "max_notional_usd": 1_000.0,
        "size_multiplier": 0.25,
        "daily_risk_budget_usd": 5_000.0,
        "max_position_count": 5,
        "max_concentration_pct": 15.0,
        "confidence_min": 0.90,         # higher bar to earn promotion
        "market_quality_min": 0.70,
        "required_governor_stance": "RISK_DOWN",
    },
    # ─── Phase 3 pilot seats ────────────────────────────────────────
    # Both start `observe` (decision-only, no orders) until the
    # verifier promotes them through shadow → toehold → auto_execute
    # on real measured decision quality.
    {
        "seat_id": "spot_short_executor",
        "instrument_type": "equity_short",
        "autonomy_mode": "observe",     # pilot mode — decision-only
        "enabled": True,
        "max_notional_usd": 1_000.0,    # small cap until promoted
        "size_multiplier": 0.25,
        "daily_risk_budget_usd": 5_000.0,
        "max_position_count": 3,        # tighter than long-side
        "max_concentration_pct": 10.0,  # short squeezes punish concentration
        "confidence_min": 0.90,         # higher bar for short side
        "market_quality_min": 0.70,
        "required_governor_stance": "RISK_DOWN",
    },
    {
        "seat_id": "options_executor",
        "instrument_type": "options",
        "autonomy_mode": "observe",     # pilot mode — decision-only
        "enabled": True,
        "max_notional_usd": 500.0,      # premium $; tightest of all seats
        "size_multiplier": 0.20,
        "daily_risk_budget_usd": 2_500.0,
        "max_position_count": 3,
        "max_concentration_pct": 10.0,
        "confidence_min": 0.92,         # highest bar — gamma+theta blow up fast
        "market_quality_min": 0.75,
        "required_governor_stance": "RISK_DOWN",
    },
]


# Default trust list:
#   - equity_executor   → alpha   (existing live equity executor)
#   - spot_short_executor → camaro (Barracuda — pilot doctrine fit)
#   - options_executor  → chevelle (Hellcat — pilot doctrine fit)
#   - crypto_executor   → (vacant; operator/verifier promotion only)
# All other seat × brain pairs start untrusted. Operator adds via
# /api/v2/seat-trust.
DEFAULT_TRUST = [
    {"seat_id": "equity_executor",       "brain_id": "camino",    "trust_level": 1.0},
    {"seat_id": "spot_short_executor",   "brain_id": "barracuda",   "trust_level": 1.0},
    {"seat_id": "options_executor",      "brain_id": "hellcat", "trust_level": 1.0},
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

    # Backfill: rows seeded before instrument_type was added (Phase 3
    # onboarding, 2026-02-19) are missing the field. Set it once per
    # row, only if absent — never overwrites operator edits.
    backfilled = 0
    for p in DEFAULT_SEAT_POLICIES:
        r = await db[PARADOX_V2_SEAT_POLICY].update_one(
            {"seat_id": p["seat_id"], "instrument_type": {"$exists": False}},
            {"$set": {"instrument_type": p["instrument_type"]}},
        )
        if r.modified_count:
            backfilled += r.modified_count
    counts["seat_policy_instrument_backfilled"] = backfilled

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
