"""Sovereign kit doctrinal smoke tests.

Runs WITHOUT a Mission Control connection. Verifies the three locks
of the observation-only door + local-state schema + core math.

Run:
    python smoke_test.py

Exit code 0 = all PASS.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import wild_adaptive_core_v2 as core  # noqa: E402
from local_state import LocalState, SCHEMA_VERSION  # noqa: E402


PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  {PASS if cond else FAIL}  {name}{(' — ' + detail) if detail else ''}")
    return cond


def test_lock1_core_defaults_false() -> bool:
    return check(
        "Lock 1: wild_adaptive_core_v2.LIVE_TRADING_ENABLED defaults False",
        core.LIVE_TRADING_ENABLED is False,
    )


def test_assert_safe_action_rejects_garbage() -> bool:
    try:
        core.assert_safe_action("LIQUIDATE_ALL")
    except RuntimeError as e:
        return check("assert_safe_action refuses unsafe actions",
                     "refused unsafe action" in str(e))
    return check("assert_safe_action refuses unsafe actions",
                 False, "did not raise")


def test_run_adaptive_core_decides_safely() -> bool:
    top = {
        "symbol": "BTC/USD",
        "price": 105.0,
        "technicals": {"sma20": 100.0, "macd": 0.5, "rsi14": 60.0},
    }
    weights = core.default_weights()
    d = core.run_adaptive_core(top, weights, account_size=0.0)
    return (
        check("decision.action ∈ {BUY,SELL,HOLD}",
              d.action in {"BUY", "SELL", "HOLD"})
        and check("decision carries confidence in [0,1]",
                  0.0 <= d.confidence <= 1.0)
        and check("confidence_origin populated for FEATURES",
                  set(d.confidence_origin.keys()) == set(core.FEATURES))
        and check("HOLD ⇒ zero notional",
                  d.notional == 0.0 if d.action == "HOLD" else True)
    )


def test_missing_sma_no_fake_bullish() -> bool:
    """Doctrine guard: missing SMA must NOT create a fake bullish signal."""
    top = {
        "symbol": "BTC/USD",
        "price": 105.0,
        "technicals": {"sma20": 0.0, "macd": 0.0, "rsi14": 50.0},
    }
    feats = core.build_features(top)
    return check("missing SMA returns trend=0 (no fake bullish)",
                 feats["trend"] == 0.0)


def test_local_state_round_trip() -> bool:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "alpha" / "state.json"
        s = LocalState(brain="alpha", path=p, mode="DTD")
        s.set_weights({"trend": 0.6, "macd": -0.2, "rsi": 0.0})
        s.set_learning_rate(0.07)
        s.append_decision({"symbol": "BTC/USD", "action": "BUY",
                           "confidence": 0.7, "resolved": False})
        s.save()
        s2 = LocalState(brain="alpha", path=p)
        ok = (
            check("state.weights round-trip",
                  s2.weights == {"trend": 0.6, "macd": -0.2, "rsi": 0.0})
            and check("state.learning_rate round-trip",
                       s2.learning_rate == 0.07)
            and check("schema_version present",
                       s2.asdict()["schema_version"] == SCHEMA_VERSION)
            and check("live_trading_enabled reasserted False on load",
                       s2.asdict()["live_trading_enabled"] is False)
        )
        # Tamper test: write True to disk and reload — must reassert False.
        raw = json.loads(p.read_text())
        raw["live_trading_enabled"] = True
        p.write_text(json.dumps(raw))
        s3 = LocalState(brain="alpha", path=p)
        ok = ok and check(
            "Lock 2: tampered live_trading_enabled=True is reasserted False",
            s3.asdict()["live_trading_enabled"] is False,
        )
        return ok


def test_local_state_rejects_out_of_bounds_weights() -> bool:
    s = LocalState(brain="alpha", path=Path(tempfile.mkdtemp()) / "x.json",
                   mode="DTD")
    try:
        s.set_weights({"trend": 99.9})
    except ValueError:
        return check("LocalState rejects weight outside [-3, +3]", True)
    return check("LocalState rejects weight outside [-3, +3]", False,
                 "set_weights accepted 99.9")


def test_update_weights_clamped() -> bool:
    w = {"trend": 2.9, "macd": -2.9, "rsi": 0.0}
    feats = {"trend": 1, "macd": -1, "rsi": 0}
    new_w = core.update_weights(w, feats, outcome=1, lr=10.0)
    return (
        check("update_weights clamps to +3",
              new_w["trend"] <= 3.0)
        and check("update_weights clamps to -3",
                   new_w["macd"] >= -3.0)
    )


def test_mode_validation() -> bool:
    try:
        LocalState(brain="alpha",
                   path=Path(tempfile.mkdtemp()) / "x.json",
                   mode="LIVE")  # not a valid mode
    except ValueError:
        return check("LocalState rejects unknown mode", True)
    return check("LocalState rejects unknown mode", False)


def main() -> int:
    print("=" * 60)
    print("RISEDUAL Sovereign Sidecar — doctrinal smoke tests")
    print("=" * 60)

    tests = [
        test_lock1_core_defaults_false,
        test_assert_safe_action_rejects_garbage,
        test_run_adaptive_core_decides_safely,
        test_missing_sma_no_fake_bullish,
        test_local_state_round_trip,
        test_local_state_rejects_out_of_bounds_weights,
        test_update_weights_clamped,
        test_mode_validation,
    ]

    results = []
    for t in tests:
        print(f"\n▸ {t.__name__}")
        results.append(t())

    passed = sum(1 for r in results if r)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"  {passed}/{total} {'PASS' if passed == total else 'FAIL'}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
