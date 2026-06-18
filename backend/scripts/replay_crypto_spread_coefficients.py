"""Replay last N crypto intents against old vs new spread coefficients.

Purpose
-------
The spread coefficient on `hold_composite` and `observe_composite` was
calibrated for equity (where spread > 25 bps = broken data). On crypto
where spreads run 50-200 bps even on liquid pairs, the equity-calibrated
weight pins HOLD/OBSERVE composites to 1.000, forcing brains to emit
HOLD on every crypto intent regardless of directional conviction.

This script takes the last N crypto intent snapshots from Mongo and
re-runs the brain decision function on each with BOTH coefficient sets:

    OLD: hold_coef=0.002  observe_coef=0.003   (current equity-tuned)
    NEW: hold_coef=0.0008 observe_coef=0.001   (proposed crypto-tuned)

Reports the action distribution for each, so the operator can validate
that the new weights produce a sane mix (target: HOLD 50-80%, not
either extreme). If the new mix is >95% BUY/SELL the change has
overcorrected; if still ~100% HOLD it hasn't moved the needle.

Run:
    python -m scripts.replay_crypto_spread_coefficients [--limit 1000]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the backend modules importable when run as a script.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE.parent.parent / ".env")

import os  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

# Add external brains module path
sys.path.insert(0, str(HERE.parent.parent.parent / "external"))
from brains.brain_core import NeutralAdversarialBrain  # noqa: E402
from shared.brain_doctrine import get_doctrine  # noqa: E402


# Spread coefficients we're comparing.
@dataclass
class Coefficients:
    name: str
    hold: float
    observe: float


OLD = Coefficients(name="OLD (equity-tuned)", hold=0.002, observe=0.003)
NEW = Coefficients(name="NEW (crypto-tuned)", hold=0.0008, observe=0.001)


def _build_brain(brain_id: str, lane: str) -> NeutralAdversarialBrain:
    return NeutralAdversarialBrain(
        brain_id=brain_id,
        display_name=brain_id.capitalize(),
        lane=lane,
        shadow_only=True,
        doctrine=get_doctrine(brain_id),
    )


def _patch_coefficients(coef: Coefficients) -> None:
    """Monkey-patch the brain core to use a specific coefficient pair.

    We patch the hold and observe formulas by overriding the
    `_build_hypotheses_doctrine` method on the class itself, then
    restore at the end. This is uglier than passing coefficients
    through the call but lets us drive the replay without touching
    brain_core's signature (which downstream callers rely on).
    """
    import brains.brain_core as bc  # noqa: WPS433

    bc._REPLAY_HOLD_COEF = coef.hold
    bc._REPLAY_OBSERVE_COEF = coef.observe


def _restore_coefficients() -> None:
    import brains.brain_core as bc  # noqa: WPS433
    if hasattr(bc, "_REPLAY_HOLD_COEF"):
        del bc._REPLAY_HOLD_COEF
    if hasattr(bc, "_REPLAY_OBSERVE_COEF"):
        del bc._REPLAY_OBSERVE_COEF


def _recompute_action(snapshot: Dict[str, Any], brain_id: str, coef: Coefficients) -> Optional[str]:
    """Re-run the brain on a stored snapshot with the given coefficients."""
    lane = "crypto"  # this replay only handles crypto
    brain = _build_brain(brain_id=brain_id, lane=lane)

    # Patch the brain's _build_hypotheses_doctrine to use our coefficients.
    # We do this by subclassing and replacing the composite math.
    original = brain._build_hypotheses_doctrine

    def patched(*, trend, rsi, setup_score, price_change, volume_change,
                volatility, spread_bps, liquidity):
        d = brain.doctrine
        trend_signal = trend
        mean_rev_signal = (50.0 - rsi) / 50.0
        breakout_signal = brain._clamp(
            setup_score + max(0.0, volume_change / 200.0), lo=0.0, hi=1.5,
        )
        momentum_signal = (price_change / 5.0) * (
            1.0 if volume_change >= 0 else 0.5
        )
        risk_penalty = (volatility * 0.6) + (spread_bps * 0.003)

        buy_composite = (
            trend_signal * d.trend_weight * 0.20
            + mean_rev_signal * d.mean_reversion_weight * 0.18
            + breakout_signal * d.breakout_weight * 0.20
            + momentum_signal * d.momentum_weight * 0.20
            - risk_penalty * d.risk_weight * 0.10
            + liquidity * 0.05
        )
        sell_composite = (
            -trend_signal * d.trend_weight * 0.20
            - mean_rev_signal * d.mean_reversion_weight * 0.18
            + breakout_signal * d.breakout_weight * 0.10
            - momentum_signal * d.momentum_weight * 0.20
            - risk_penalty * d.risk_weight * 0.10
            + liquidity * 0.05
        )
        # ─── Coefficient swap happens here ─────────────────
        hold_composite = (
            0.45
            + volatility * 0.20
            + spread_bps * coef.hold
            + (1.0 - liquidity) * 0.15
            - abs(trend_signal) * d.trend_weight * 0.08
            - abs(momentum_signal) * d.momentum_weight * 0.05
        )
        observe_composite = (
            0.40
            + spread_bps * coef.observe
            + volatility * 0.12
            + (1.0 - liquidity) * 0.10
        )

        agg = float(d.aggression)
        buy_score = brain._clamp(0.50 + buy_composite * agg)
        sell_score = brain._clamp(0.50 + sell_composite * agg)
        hold_score = brain._clamp(hold_composite)
        observe_score = brain._clamp(observe_composite)

        from brains.brain_core import Hypothesis
        return [
            Hypothesis(name="hypothesis_buy", action="BUY",
                       score=buy_score, confidence=buy_score, reasons=[]),
            Hypothesis(name="hypothesis_sell", action="SELL",
                       score=sell_score, confidence=sell_score, reasons=[]),
            Hypothesis(name="hypothesis_hold", action="HOLD",
                       score=hold_score, confidence=hold_score, reasons=[]),
            Hypothesis(name="hypothesis_observe", action="OBSERVE",
                       score=observe_score, confidence=observe_score, reasons=[]),
        ]

    brain._build_hypotheses_doctrine = patched

    # Extract the fields the brain needs from the stored snapshot.
    try:
        intent = brain.evaluate(
            symbol=snapshot.get("symbol", "?"),
            snapshot={
                "symbol": snapshot.get("symbol"),
                "price": snapshot.get("price", 0),
                "price_change_pct": snapshot.get("price_change_pct", 0),
                "volume_change_pct": snapshot.get("volume_change_pct", 0),
                "rsi": snapshot.get("rsi", 50),
                "spread_bps": snapshot.get("spread_bps", 0),
                "volatility": snapshot.get("volatility", 0),
                "trend_score": snapshot.get("trend_score", 0),
                "liquidity_score": snapshot.get("liquidity_score", 1.0),
                "market_regime": snapshot.get("market_regime", "chop"),
                "setup_score": snapshot.get("setup_score", 0),
                "pattern": snapshot.get("pattern", "none"),
            },
        )
        return intent.action
    except Exception:  # noqa: BLE001
        return None


async def main(limit: int) -> int:
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]

    cursor = db["shared_intents"].find(
        {"lane": "crypto"},
        sort=[("_id", -1)],
        limit=limit,
    )
    rows = await cursor.to_list(length=limit)
    print(f"Fetched {len(rows)} crypto intents from shared_intents")

    counters: Dict[str, Counter] = {"OLD": Counter(), "NEW": Counter()}
    per_symbol: Dict[str, Dict[str, Counter]] = defaultdict(
        lambda: {"OLD": Counter(), "NEW": Counter()}
    )
    no_snapshot = 0
    for r in rows:
        snap = r.get("snapshot") or {}
        if not snap or "spread_bps" not in snap:
            no_snapshot += 1
            continue
        brain_id = (r.get("canonical") or r.get("stack") or "").lower()
        if brain_id.startswith("crypto:"):
            # Older rows used the symbol as canonical; pull brain from rationale instead
            r_text = r.get("rationale") or ""
            if "brain_id=" in r_text:
                brain_id = r_text.split("brain_id=")[1].split()[0]
        if brain_id not in ("camino", "barracuda", "hellcat", "gto"):
            continue

        old_action = _recompute_action(snap, brain_id, OLD)
        new_action = _recompute_action(snap, brain_id, NEW)
        if old_action is None or new_action is None:
            continue
        counters["OLD"][old_action] += 1
        counters["NEW"][new_action] += 1

        sym = r.get("symbol", "?")
        per_symbol[sym]["OLD"][old_action] += 1
        per_symbol[sym]["NEW"][new_action] += 1

    print(f"Rows skipped (no snapshot or unsupported brain): {no_snapshot}")
    print()
    print("=" * 76)
    print(f"{'Action':<10} {'OLD count':>12} {'OLD %':>10} {'NEW count':>12} {'NEW %':>10}")
    print("-" * 76)
    total_old = sum(counters["OLD"].values()) or 1
    total_new = sum(counters["NEW"].values()) or 1
    for action in ("BUY", "SELL", "HOLD", "OBSERVE"):
        o = counters["OLD"][action]
        n = counters["NEW"][action]
        print(
            f"{action:<10} {o:>12} {100*o/total_old:>9.1f}% "
            f"{n:>12} {100*n/total_new:>9.1f}%"
        )
    print("=" * 76)
    print(f"{'TOTAL':<10} {total_old:>12} {'':>10} {total_new:>12}")
    print()
    print("Per-symbol breakdown (NEW coefficients):")
    for sym, sides in sorted(per_symbol.items()):
        new = sides["NEW"]
        total = sum(new.values()) or 1
        parts = " ".join(
            f"{a}={100*new[a]/total:.0f}%" for a in ("BUY","SELL","HOLD","OBSERVE") if new[a]
        )
        print(f"  {sym:<12} n={total:<4}  {parts}")

    cli.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(limit=args.limit)))
