"""Replay last N crypto intents against three brain logic configurations.

Validates Move 1 (OBSERVE → market_quality modifier) + Move 2
(lane-aware min_gap) restructure before deploying to prod.

Compares three configurations on the same N crypto snapshots:

    OLD:      OBSERVE competes in argmax, min_gap from doctrine (0.06-0.10)
    MOVE_1:   OBSERVE removed from argmax, min_gap unchanged
    MOVE_1_2: OBSERVE removed + crypto min_gap dropped to 0.03

This is a self-contained simulator — it reads stored snapshots from
Mongo and applies the brain math directly. No live brain process is
modified.

Targets:
    HOLD 50-80% (not 100%, not 5%)
    BUY/SELL spread across brains, not concentrated
    Doctrine ordering preserved (Barracuda → Hellcat min_confidence)

Run:
    python -m scripts.replay_crypto_spread_coefficients [--limit 1000]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

# Make backend importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.brain_doctrine import get_doctrine  # noqa: E402


# ─── Brain math (copied from external/brains/brain_core.py) ────────


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x != x or x in (float("inf"), float("-inf")):
        return lo
    return max(lo, min(hi, x))


def _composites(snap: Dict[str, Any], brain_id: str) -> Dict[str, float]:
    """Compute the four composite scores for a brain on this snapshot."""
    d = get_doctrine(brain_id)
    trend = float(snap.get("trend_score", 0.0) or 0.0)
    rsi = float(snap.get("rsi", 50.0) or 50.0)
    setup_score = float(snap.get("setup_score", 0.0) or 0.0)
    price_change = float(snap.get("price_change_pct", 0.0) or 0.0)
    volume_change = float(snap.get("volume_change_pct", 0.0) or 0.0)
    volatility = float(snap.get("volatility", 0.0) or 0.0)
    spread_bps = float(snap.get("spread_bps", 0.0) or 0.0)
    liquidity = float(snap.get("liquidity_score", 1.0) or 1.0)

    trend_signal = trend
    mean_rev_signal = (50.0 - rsi) / 50.0
    breakout_signal = _clamp(
        setup_score + max(0.0, volume_change / 200.0), 0.0, 1.5,
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
    hold_composite = (
        0.45
        + volatility * 0.20
        + spread_bps * 0.002
        + (1.0 - liquidity) * 0.15
        - abs(trend_signal) * d.trend_weight * 0.08
        - abs(momentum_signal) * d.momentum_weight * 0.05
    )
    observe_composite = (
        0.40
        + spread_bps * 0.003
        + volatility * 0.12
        + (1.0 - liquidity) * 0.10
    )
    agg = float(d.aggression)
    return {
        "BUY": _clamp(0.50 + buy_composite * agg),
        "SELL": _clamp(0.50 + sell_composite * agg),
        "HOLD": _clamp(hold_composite),
        "OBSERVE": _clamp(observe_composite),
    }


def _decide(scores: Dict[str, float], brain_id: str, *,
            include_observe_in_argmax: bool, min_gap_override: float = None) -> str:
    """Apply the brain's final-action rule under a given configuration."""
    d = get_doctrine(brain_id)
    min_commit = d.min_confidence
    min_gap = min_gap_override if min_gap_override is not None else d.min_gap

    pool = ("BUY", "SELL", "HOLD", "OBSERVE") if include_observe_in_argmax else ("BUY", "SELL", "HOLD")
    ranked = sorted(pool, key=lambda k: scores[k], reverse=True)
    winner, runner_up = ranked[0], ranked[1]
    gap = scores[winner] - scores[runner_up]

    if scores[winner] < min_commit:
        # OLD path emitted OBSERVE; new path emits HOLD (OBSERVE no
        # longer a direction). For apples-to-apples, OLD also gets
        # HOLD here so we measure the gap-rule effect cleanly.
        return "OBSERVE" if include_observe_in_argmax else "HOLD"
    if gap < min_gap:
        return "HOLD"
    return winner


# ─── Replay driver ─────────────────────────────────────────────────


CONFIGS = {
    "OLD":      dict(include_observe_in_argmax=True,  min_gap_override=None),
    "MOVE_1":   dict(include_observe_in_argmax=False, min_gap_override=None),
    "MOVE_1_2": dict(include_observe_in_argmax=False, min_gap_override=0.03),
}


async def main(limit: int) -> int:
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    cursor = db["shared_intents"].find(
        {"lane": "crypto"}, sort=[("_id", -1)], limit=limit,
    )
    rows = await cursor.to_list(length=limit)
    print(f"Fetched {len(rows)} crypto intents from shared_intents")

    counters: Dict[str, Counter] = {k: Counter() for k in CONFIGS}
    per_brain: Dict[str, Dict[str, Counter]] = defaultdict(
        lambda: {k: Counter() for k in CONFIGS}
    )
    per_symbol_move12: Dict[str, Counter] = defaultdict(Counter)
    skipped = 0

    for r in rows:
        snap = r.get("snapshot") or {}
        if not snap or "spread_bps" not in snap:
            skipped += 1
            continue
        brain_id = (r.get("canonical") or "").lower()
        if brain_id.startswith("crypto:") or brain_id not in (
            "camino", "barracuda", "hellcat", "gto"
        ):
            r_text = r.get("rationale") or ""
            if "brain_id=" in r_text:
                brain_id = r_text.split("brain_id=")[1].split()[0]
        if brain_id not in ("camino", "barracuda", "hellcat", "gto"):
            skipped += 1
            continue

        scores = _composites(snap, brain_id)
        for cfg_name, cfg in CONFIGS.items():
            action = _decide(scores, brain_id, **cfg)
            counters[cfg_name][action] += 1
            per_brain[brain_id][cfg_name][action] += 1

        sym = r.get("symbol", "?")
        per_symbol_move12[sym][_decide(scores, brain_id, **CONFIGS["MOVE_1_2"])] += 1

    print(f"Skipped (no snapshot or unsupported brain): {skipped}")
    print()
    total = {k: sum(c.values()) or 1 for k, c in counters.items()}

    print("=" * 88)
    print(f"{'Action':<10}  "
          f"{'OLD':>10} {'OLD %':>7}  "
          f"{'MOVE_1':>10} {'MOVE_1 %':>9}  "
          f"{'MOVE_1_2':>10} {'M_1_2 %':>9}")
    print("-" * 88)
    for action in ("BUY", "SELL", "HOLD", "OBSERVE"):
        o, m1, m12 = counters["OLD"][action], counters["MOVE_1"][action], counters["MOVE_1_2"][action]
        print(
            f"{action:<10}  "
            f"{o:>10} {100*o/total['OLD']:>6.1f}%  "
            f"{m1:>10} {100*m1/total['MOVE_1']:>8.1f}%  "
            f"{m12:>10} {100*m12/total['MOVE_1_2']:>8.1f}%"
        )
    print("=" * 88)
    print(f"{'TOTAL':<10}  "
          f"{total['OLD']:>10}          "
          f"{total['MOVE_1']:>10}            "
          f"{total['MOVE_1_2']:>10}")
    print()

    print("Per-brain action mix (under MOVE_1_2 — the proposed prod config):")
    print("-" * 88)
    for brain_id in ("barracuda", "gto", "camino", "hellcat"):
        d = get_doctrine(brain_id)
        c = per_brain[brain_id]["MOVE_1_2"]
        n = sum(c.values()) or 1
        parts = " ".join(
            f"{a}={100*c[a]/n:.0f}%" for a in ("BUY", "SELL", "HOLD") if c[a]
        )
        print(
            f"  {brain_id:<10} ({d.doctrine:<15} min_conf={d.min_confidence:.2f})  "
            f"n={n:<4}  {parts}"
        )
    print()
    print("Per-symbol breakdown (MOVE_1_2):")
    for sym, c in sorted(per_symbol_move12.items()):
        n = sum(c.values()) or 1
        parts = " ".join(
            f"{a}={100*c[a]/n:.0f}%" for a in ("BUY", "SELL", "HOLD") if c[a]
        )
        print(f"  {sym:<12} n={n:<4}  {parts}")

    cli.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(limit=args.limit)))
