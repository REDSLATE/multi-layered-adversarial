"""Hellcat's strategy: execution safety.

Doctrine (operator, 2026-07-12): "Hellcat should not simply be
another directional strategy. Its strongest identity is as the
brain that asks whether the setup is tradable under spread,
liquidity, volatility, and news conditions."

Execution-safety flow:

  Step 1 — EXECUTION QUALITY GATE (first, always).
    If ANY of:
      • spread_bps too wide for lane
      • volatility spike
      • liquidity_score too low
    then emit HOLD with an EXEC_* reason. This is a VETO tag —
    Hellcat's HOLDs are not "no opinion", they're "the venue is
    hostile."

  Step 2 — WEAK DIRECTIONAL READ.
    Only when execution is clean, look at price_change_pct's
    sign for a low-conviction direction. Hellcat's own direction
    is intentionally quiet: it's saying "if you MUST trade, this
    is the lean." The council does the loud directional talking.

Hellcat's distinctness: it is the ONLY brain that HOLDs on
execution grounds. When spreads widen at open or vol spikes on
news, Camino/GTO/Barracuda may still be shouting directions —
Hellcat quietly vetoes. That's the point.
"""
from __future__ import annotations

from typing import ClassVar

from mc_brains.strategies import StrategyResult
from mc_pulse.snapshot import MarketSnapshot


class ExecutionSafetyStrategy:
    """Hellcat's strategy — see module docstring."""

    reason_code_family: ClassVar[str] = "EXEC"

    # Per-lane spread ceilings. Crypto spreads are generally wider
    # than equity; a 30bps equity spread is bad, a 30bps crypto
    # spread is normal.
    MAX_SPREAD_BPS_EQUITY = 25.0
    MAX_SPREAD_BPS_CRYPTO = 60.0

    # Volatility ceiling — realized vol above this is a spike.
    # The scale here matches the pulse feature builder's `volatility`
    # convention (roughly annualized fraction; 0.05 = 5%).
    MAX_VOLATILITY = 0.08

    # Liquidity floor — the feature is 0..1.
    MIN_LIQUIDITY = 0.35

    # Weak-directional threshold.
    WEAK_PRICE_MAG = 0.15

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        feats = snapshot.feature_snapshot or {}
        spread = _f(feats.get("spread_bps"))
        vol = _f(feats.get("volatility"))
        liq = _f(feats.get("liquidity_score"))
        pc = _f(feats.get("price_change_pct"))
        lane = (snapshot.lane or "equity").lower()

        # ── Step 1: execution quality gate ──
        vetos: list[str] = []
        max_spread = (
            self.MAX_SPREAD_BPS_CRYPTO if lane == "crypto"
            else self.MAX_SPREAD_BPS_EQUITY
        )

        if spread is not None and spread > max_spread:
            vetos.append("EXEC_SPREAD_TOO_WIDE")
        if vol is not None and vol > self.MAX_VOLATILITY:
            vetos.append("EXEC_VOL_SPIKE")
        if liq is not None and liq < self.MIN_LIQUIDITY:
            vetos.append("EXEC_LIQUIDITY_LOW")

        if vetos:
            # Confidence 0.5-0.8 because these are HIGH-conviction
            # HOLDs — Hellcat is confident the venue is hostile.
            n = len(vetos)
            conf = min(0.85, 0.55 + 0.10 * n)
            return StrategyResult(
                action="HOLD", confidence=round(conf, 4),
                reason_codes=tuple(vetos + ["EXEC_HOLD_VETO"]),
                edge_evidence={
                    "spread_bps": spread, "volatility": vol,
                    "liquidity_score": liq, "lane": lane,
                    "veto_count": n,
                },
            )

        # ── Step 2: weak directional read (execution is clean) ──
        if pc is None:
            return StrategyResult(
                action="HOLD", confidence=0.20,
                reason_codes=("EXEC_CLEAN_NO_DIRECTION",),
                edge_evidence={"execution": "clean", "price_change_pct": None},
            )

        if abs(pc) < self.WEAK_PRICE_MAG:
            return StrategyResult(
                action="HOLD", confidence=0.25,
                reason_codes=("EXEC_CLEAN_FLAT",),
                edge_evidence={"execution": "clean", "price_change_pct": pc},
            )

        # Direction is a WEAK read — capped at 0.55 by design so
        # Hellcat never dominates the council on directional
        # conviction. Its high-conviction outputs are HOLDs.
        conf = min(0.55, 0.30 + abs(pc) / 5.0)
        if pc > 0:
            return StrategyResult(
                action="BUY", confidence=round(conf, 4),
                reason_codes=("EXEC_CLEAN_UP", "EXEC_WEAK_DIRECTIONAL"),
                edge_evidence={"execution": "clean", "price_change_pct": pc},
            )
        return StrategyResult(
            action="SELL", confidence=round(conf, 4),
            reason_codes=("EXEC_CLEAN_DOWN", "EXEC_WEAK_DIRECTIONAL"),
            edge_evidence={"execution": "clean", "price_change_pct": pc},
        )


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
