"""RTH Opportunity Scanner — deterministic tests (operator spec #13).
Scanner is advisory only: no broker order access, no intent emission."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.hotpath import outbox as ob
from shared.scanner import store
from shared.scanner.rth_scanner import (
    DEFAULT_POLICY,
    brain_affinity,
    classify,
    evaluate_symbol,
    opportunity_score,
    score_components,
)
from shared.scanner.universe500 import LEVERAGED_INVERSE, discovery_universe


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    ob.reset_for_tests(tmp_path / "hp.sqlite")
    store.reset_for_tests()
    yield
    ob.reset_for_tests("/app/backend/data/hotpath.sqlite")
    store.reset_for_tests()


def _bars(n=40, price=100.0, vol=500_000, trend=0.0, last_vol_mult=1.0,
          age_min=2.0):
    now = datetime.now(timezone.utc)
    out = []
    for i in range(n):
        c = price * (1 + trend * i / n)
        v = vol * (last_vol_mult if i >= n - 6 else 1.0)
        ts = (now - timedelta(minutes=5 * (n - 1 - i) + age_min)).isoformat()
        out.append({"ts": ts, "o": c * 0.999, "h": c * 1.004,
                    "l": c * 0.996, "c": c, "v": v})
    return out


# ── universe / exclusions ───────────────────────────────────────────

def test_universe_excludes_leveraged_by_default():
    u = discovery_universe({})
    assert "TQQQ" not in u and "SPY" in u and "AAPL" in u
    assert 300 <= len(u) <= 600
    u2 = discovery_universe({"allow_leveraged": True, "extra_symbols": ["TQQQ"]})
    assert "TQQQ" in u2


def test_universe_extra_and_exclude_knobs():
    u = discovery_universe({"extra_symbols": ["ZZZZ"], "exclude_symbols": ["AAPL"]})
    assert "ZZZZ" in u and "AAPL" not in u


def test_leveraged_rejected_at_evaluation():
    sym = next(iter(LEVERAGED_INVERSE))
    cand, why = evaluate_symbol(sym, _bars(), dict(DEFAULT_POLICY))
    assert cand is None and why == "leveraged_inverse"


# ── hard filters ────────────────────────────────────────────────────

def test_stale_data_rejected():
    cand, why = evaluate_symbol("AAPL", _bars(age_min=45.0), dict(DEFAULT_POLICY))
    assert cand is None and why == "stale_data"


def test_low_dollar_volume_rejected():
    cand, why = evaluate_symbol("AAPL", _bars(vol=1_000), dict(DEFAULT_POLICY))
    assert cand is None and why == "low_dollar_volume"


def test_below_min_price_rejected():
    cand, why = evaluate_symbol(
        "AAPL", _bars(price=2.0, vol=50_000_000), dict(DEFAULT_POLICY),
    )
    assert cand is None and why == "below_min_price"


def test_insufficient_bars_rejected():
    cand, why = evaluate_symbol("AAPL", _bars(n=8), dict(DEFAULT_POLICY))
    assert cand is None and why == "insufficient_bars"


# ── scoring / ranking / classification ──────────────────────────────

def test_admission_produces_full_candidate_record():
    cand, why = evaluate_symbol(
        "AAPL", _bars(vol=2_000_000, trend=0.02, last_vol_mult=3.0),
        dict(DEFAULT_POLICY),
    )
    assert why is None, why
    for field in ("symbol", "opportunity_score", "components", "brain_affinity",
                  "classification", "expires_at", "scanned_at", "price",
                  "hourly_dollar_vol", "bar_age_min", "inclusion_reason"):
        assert field in cand
    assert set(cand["brain_affinity"]) == {"barracuda", "camino", "hellcat", "gto"}


def test_hot_mover_outscores_quiet_name():
    quiet = score_components(_bars(vol=1_000_000))
    hot = score_components(_bars(vol=1_000_000, trend=0.03, last_vol_mult=4.0))
    assert opportunity_score(hot) > opportunity_score(quiet)


def test_gto_affinity_rises_with_momentum():
    quiet = brain_affinity(score_components(_bars(vol=1_000_000)))
    hot = brain_affinity(score_components(
        _bars(vol=1_000_000, trend=0.04, last_vol_mult=4.0)))
    assert hot["gto"] > quiet["gto"]


def test_classification_routing():
    assert classify("SPY", 50_000_000) == "etf"
    assert classify("AAPL", 50_000_000) == "large_cap"
    assert classify("IONQ", 3_000_000) == "growth_equity"


# ── store: expiry / ranking / invalidation ──────────────────────────

def test_store_ranking_and_expiry():
    now = datetime.now(timezone.utc)
    store.upsert_candidates([
        {"symbol": "AAA", "opportunity_score": 0.9,
         "expires_at": (now + timedelta(minutes=10)).isoformat()},
        {"symbol": "BBB", "opportunity_score": 0.5,
         "expires_at": (now + timedelta(minutes=10)).isoformat()},
        {"symbol": "OLD", "opportunity_score": 0.99,
         "expires_at": (now - timedelta(minutes=1)).isoformat()},
    ])
    live = store.live_candidates()
    assert [c["symbol"] for c in live] == ["AAA", "BBB"], "expired must not serve"
    assert store.purge_expired() == 1
    store.invalidate("AAA")
    assert [c["symbol"] for c in store.live_candidates()] == ["BBB"]


# ── advisory boundary ───────────────────────────────────────────────

def test_scanner_has_no_broker_order_access():
    import inspect
    import shared.scanner.rth_scanner as mod
    src = inspect.getsource(mod)
    for forbidden in ("submit_market_order", "submit_limit_order",
                      "place_order", "submit_intent"):
        assert forbidden not in src, f"scanner must never call {forbidden}"


def test_refresher_merges_scanner_rows_with_bypass():
    from shared.universe.refresher import (
        _apply_hysteresis, _dedupe_and_merge, _scanner_discovery_candidates,
    )
    now = datetime.now(timezone.utc)
    store.upsert_candidates([{
        "symbol": "AMD", "opportunity_score": 0.87, "price": 150.0,
        "momentum_pct": 2.1,
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
    }])
    rows = _scanner_discovery_candidates()
    assert rows and rows[0]["canonical_symbol"] == "AMD"
    merged = _dedupe_and_merge(rows)
    admitted = _apply_hysteresis(merged, set(), admit_cap=0)
    assert admitted and admitted[0]["_admit_reason"] == "rth_scanner"
