"""Public-API trust + tier dependency.

Two headers, validated as a unit:

    X-RiseDual-Token       — opaque bearer; must match RISEDUAL_PUBLIC_TOKEN
                             env var on MC. risedual.ai's backend stores
                             this in its own env and forwards on outbound
                             calls. If it leaks, rotate the env var on MC.

    X-RiseDual-User-Tier   — free | starter | pro | pro_max. risedual.ai
                             knows the caller's tier; MC trusts it for
                             gating (sanitization of free-tier rows).

Defaults:
  * Tier missing or empty  → free
  * Tier unknown           → 422
  * Token missing/wrong    → 401
  * Token env-var missing  → 503 (public API not configured)
"""
from __future__ import annotations

import os
from typing import Literal

from fastapi import Header, HTTPException


TierT = Literal["free", "starter", "pro", "pro_max"]
VALID_TIERS = frozenset({"free", "starter", "pro", "pro_max"})

# Per the operator: starter is also a non-paid tier. Only pro and pro_max
# get unlimited gated features (war_room, ai_chat). MC mirrors this so
# free-tier sanitization works the same way regardless of which non-paid
# label risedual.ai assigned the user.
UNPAID_TIERS = frozenset({"free", "starter"})
UNLIMITED_TIERS = frozenset({"pro", "pro_max"})


class PublicCaller:
    """Carries the trusted view of who called us."""

    __slots__ = ("tier", "is_paid", "is_unlimited")

    def __init__(self, tier: str):
        self.tier: str = tier
        self.is_paid: bool = tier not in UNPAID_TIERS
        self.is_unlimited: bool = tier in UNLIMITED_TIERS

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "is_paid": self.is_paid,
            "is_unlimited": self.is_unlimited,
        }


def public_trust_required(
    x_risedual_token: str | None = Header(default=None, alias="X-RiseDual-Token"),
    x_risedual_user_tier: str | None = Header(
        default=None, alias="X-RiseDual-User-Tier",
    ),
) -> PublicCaller:
    expected = os.environ.get("RISEDUAL_PUBLIC_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="public API not configured (RISEDUAL_PUBLIC_TOKEN unset)",
        )
    if not x_risedual_token or x_risedual_token != expected:
        raise HTTPException(status_code=401, detail="invalid public trust token")

    tier_raw = (x_risedual_user_tier or "free").strip().lower()
    if tier_raw not in VALID_TIERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"X-RiseDual-User-Tier must be one of "
                f"{sorted(VALID_TIERS)}; got {tier_raw!r}"
            ),
        )
    return PublicCaller(tier=tier_raw)
