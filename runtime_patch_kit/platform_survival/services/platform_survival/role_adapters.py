"""Role adapters — each brain emits in its canonical shape.

Doctrine (2026-05-18 operator spec):
    Brains speak → MC classifies → MC governs → MC routes

These adapters are the canonical shape every brain ships TO MC. The
classifier (`shared.intent_contract.classify_brain_intent` on MC's
side) reads from these shapes and decides executable vs advisory.

Sidecars never decide whether their own emission is executable. They
only package the shape. MC owns the policy.

USE INSIDE EACH BRAIN'S SIDECAR — not on MC. MC's classifier reads
the payload these functions produce.
"""
from __future__ import annotations

from typing import Any, Dict


# ─────────────────────── Camaro — executor style ─────────────────────


def camaro_emit_crypto_intent(
    symbol: str,
    direction: str,
    confidence: float,
    notional_usd: float,
) -> Dict[str, Any]:
    """Camaro is the crypto executor candidate. It speaks in
    BUY/SELL/HOLD. MC classifies HOLD as advisory_only and skips it;
    BUY/SELL above the exec floor become executable candidates."""
    return {
        "brain": "camaro",
        "role": "crypto_executor",
        "intent_type": "EXECUTION_INTENT",
        "lane": "crypto",
        "symbol": symbol,
        "direction": direction.upper(),
        "raw_confidence": float(confidence),
        "notional_usd": float(notional_usd),
    }


# ─────────────────────── Alpha — strategist style ────────────────────


def alpha_emit_opinion(
    symbol: str,
    lane: str,
    direction: str,
    confidence: float,
) -> Dict[str, Any]:
    """Alpha emits opinions. Opinions are advisory UNLESS Alpha holds
    the executor seat for the lane (MC checks this via the roster).
    Alpha never claims executor authority in the payload itself."""
    return {
        "brain": "alpha",
        "role": "strategist",
        "intent_type": "OPINION",
        "lane": lane.lower(),
        "symbol": symbol,
        "direction": direction.upper(),
        "raw_confidence": float(confidence),
    }


# ─────────────────────── Chevelle — governor style ───────────────────


def chevelle_emit_authority(
    symbol: str,
    lane: str,
    status: str,
    reason: str,
    confidence: float,
) -> Dict[str, Any]:
    """Chevelle emits authority calls — ALLOW / WARN / BLOCK with a
    reason code. MC's governor_policy translates `status` + `reason`
    into HARD_BLOCK vs RISK_DOWN_ONLY using the FATAL_GOVERNOR_REASONS
    taxonomy. Chevelle's silence is RISK_DOWN, not BLOCK — only
    GOVERNOR_HARD_VETO (and the structural safety reasons) kill the
    trade outright."""
    return {
        "brain": "chevelle",
        "role": "governor",
        "intent_type": "GOVERNOR_AUTHORITY",
        "lane": lane.lower(),
        "symbol": symbol,
        "status": status.upper(),     # ALLOW / WARN / BLOCK
        "reason": reason.upper(),
        "confidence": float(confidence),
    }


# ─────────────────────── REDEYE — opponent style ─────────────────────


def redeye_emit_opposition(
    symbol: str,
    lane: str,
    direction: str,
    confidence: float,
    opposes: bool,
) -> Dict[str, Any]:
    """REDEYE files oppositions — adversarial evidence to a primary
    intent. Opposition alone does NOT kill the trade (per doctrine);
    it counts as objection weight in the council's adversary lane."""
    return {
        "brain": "redeye",
        "role": "opponent",
        "intent_type": "OPPOSITION",
        "lane": lane.lower(),
        "symbol": symbol,
        "direction": direction.upper(),
        "raw_confidence": float(confidence),
        "opposes_primary": bool(opposes),
    }
