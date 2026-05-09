"""Standalone smoke test for the REDEYE short-side bridge.

Run from this folder:
    python3 smoke_test.py

This mirrors the documented CLI invocation:
    python -m risedual shorts \
      --symbol TSLA --price-change-pct -2.4 --rsi 39 --macd-hist -0.22 \
      --volume-ratio 1.8 --below-sma-20 --below-sma-50 --failed-bounce \
      --model-score 0.82

Exits non-zero on any contract violation (action != SHORT, reports_to != CAMARO,
may_execute != False, may_override_alpha != False, final_authority != CAMARO).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `services.redeye_short_bridge` importable without packaging the kit.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.redeye_short_bridge import (  # noqa: E402
    build_redeye_short_signal,
    export_for_camaro,
)


def main() -> int:
    features = {
        "price_change_pct": -2.4,
        "rsi_14": 39,
        "macd_hist": -0.22,
        "volume_ratio": 1.8,
        "below_sma_20": True,
        "below_sma_50": True,
        "failed_bounce": True,
        "liquidity_ok": True,
        "borrow_ok": True,
    }

    signal = build_redeye_short_signal("TSLA", features, model_score=0.82)
    payload = export_for_camaro(signal)
    print(json.dumps(payload, indent=2, sort_keys=True))

    # Hard contract assertions — REDEYE must NEVER bypass Camaro.
    contract = payload["camaro_contract"]
    failures = []
    if payload["engine"] != "REDEYE":
        failures.append(f"engine != REDEYE (got {payload['engine']})")
    if payload["reports_to"] != "CAMARO":
        failures.append(f"reports_to != CAMARO (got {payload['reports_to']})")
    if payload["action"] != "SHORT":
        failures.append(f"action != SHORT (got {payload['action']})")
    if contract["may_execute"] is not False:
        failures.append("may_execute must be False")
    if contract["may_override_alpha"] is not False:
        failures.append("may_override_alpha must be False")
    if contract["final_authority"] != "CAMARO":
        failures.append(f"final_authority != CAMARO (got {contract['final_authority']})")
    if contract["source"] != "REDEYE":
        failures.append(f"source != REDEYE (got {contract['source']})")
    if contract["role"] != "short_side_advisor":
        failures.append(f"role != short_side_advisor (got {contract['role']})")

    # Sanity: the documented bullish scenario should be HOLD, not SHORT.
    bullish = build_redeye_short_signal(
        "AAPL",
        {
            "price_change_pct": 2.5,
            "rsi_14": 65,
            "macd_hist": 0.4,
            "volume_ratio": 1.0,
            "below_sma_20": False,
            "below_sma_50": False,
            "failed_bounce": False,
            "liquidity_ok": True,
            "borrow_ok": True,
        },
    )
    if bullish.action != "HOLD":
        failures.append(f"bullish scenario must HOLD (got {bullish.action})")

    # Sanity: borrow_block must force HOLD even if bear_score is high.
    blocked = build_redeye_short_signal(
        "GME",
        {**features, "borrow_ok": False},
        model_score=0.95,
    )
    if blocked.action != "HOLD":
        failures.append("borrow_block must force HOLD")
    if "borrow_block" not in blocked.reason:
        failures.append("borrow_block must appear in reason")

    if failures:
        print("\nCONTRACT VIOLATIONS:", file=sys.stderr)
        for f in failures:
            print(f" - {f}", file=sys.stderr)
        return 1

    print("\nOK: REDEYE → Camaro contract holds. SHORT/HOLD gates work. Borrow-block enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
