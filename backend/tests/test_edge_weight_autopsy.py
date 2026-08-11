"""Edge Weight (sizing-only multiplier) + Drawdown Autopsy tests
(2026-06 operator directive — measurement first, never a gate)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.forensics.drawdown_autopsy import compute_autopsy  # noqa: E402
from shared.risk_sizer import edge_weight as ew  # noqa: E402

pytestmark = pytest.mark.tripwire


@pytest.fixture(autouse=True)
def _fresh():
    ew.reset_for_tests()
    yield
    ew.reset_for_tests()


def _cfg(monkeypatch, **over):
    async def _get():
        return {**ew.DEFAULTS, **over}
    monkeypatch.setattr(ew, "get_config", _get)


def _stats(monkeypatch, stats):
    async def _get(lane, cfg):
        return stats
    monkeypatch.setattr(ew, "slice_stats", _get)


# ───────────────────────── edge weight ──────────────────────────────

@pytest.mark.asyncio
async def test_strong_slices_full_weight(monkeypatch):
    _cfg(monkeypatch)
    _stats(monkeypatch, {
        "hour": {ew._hour_bucket(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)): {"n": 100, "expectancy_net": 1.5}},
        "weekday": {__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%a"): {"n": 150, "expectancy_net": 2.0}},
        "symbol": {"BICO/USD": {"n": 213, "expectancy_net": 1.2}},
    })
    w, r = await ew.get_edge_weight({"lane": "crypto", "symbol": "BICO/USD"})
    assert w == 1.0
    assert r["note"] == "sizing influence only — never a gate"


@pytest.mark.asyncio
async def test_weak_slices_floor_never_zero(monkeypatch):
    _cfg(monkeypatch)
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    _stats(monkeypatch, {
        "hour": {ew._hour_bucket(now): {"n": 100, "expectancy_net": -2.0}},
        "weekday": {now.strftime("%a"): {"n": 150, "expectancy_net": -1.0}},
        "symbol": {"BAD/USD": {"n": 60, "expectancy_net": -3.0}},
    })
    w, _ = await ew.get_edge_weight({"lane": "crypto", "symbol": "BAD/USD",
                                     "confidence": 0.3})
    assert w == pytest.approx(0.25)  # hard floor — trade still happens


@pytest.mark.asyncio
async def test_no_data_is_neutral_not_deny(monkeypatch):
    _cfg(monkeypatch)
    _stats(monkeypatch, {"hour": {}, "weekday": {}, "symbol": {}})
    w, r = await ew.get_edge_weight({"lane": "crypto", "symbol": "NEW/USD"})
    assert w == pytest.approx(0.65)  # neutral, exploratory — never 0
    assert all(c["score"] == 0.65 for c in r["components"].values())


@pytest.mark.asyncio
async def test_insufficient_n_treated_as_neutral(monkeypatch):
    # 975 obs is a hypothesis, not doctrine: thin slices don't bias
    _cfg(monkeypatch)
    _stats(monkeypatch, {"hour": {}, "weekday": {},
                         "symbol": {"THIN/USD": {"n": 5, "expectancy_net": 9.0}}})
    w, r = await ew.get_edge_weight({"lane": "crypto", "symbol": "THIN/USD"})
    assert r["components"]["symbol"]["score"] == 0.65
    assert w == pytest.approx(0.65)


@pytest.mark.asyncio
async def test_live_confidence_lifts_weight(monkeypatch):
    # current market evidence beats history: strong setup lifts weight
    _cfg(monkeypatch)
    _stats(monkeypatch, {"hour": {}, "weekday": {}, "symbol": {}})
    w_low, _ = await ew.get_edge_weight({"lane": "crypto", "symbol": "X/USD"})
    w_hi, r = await ew.get_edge_weight({"lane": "crypto", "symbol": "X/USD",
                                        "confidence": 0.9})
    assert w_hi == pytest.approx(w_low + 0.15)
    assert r["confidence_adj"] == 0.15


@pytest.mark.asyncio
async def test_disabled_and_failure_are_one_x(monkeypatch):
    _cfg(monkeypatch, enabled=False)
    w, r = await ew.get_edge_weight({"lane": "crypto"})
    assert w == 1.0 and r == {"enabled": False, "weight": 1.0}

    _cfg(monkeypatch)
    async def _boom(lane, cfg):
        raise RuntimeError("mongo down")
    monkeypatch.setattr(ew, "slice_stats", _boom)
    w2, r2 = await ew.get_edge_weight({"lane": "crypto"})
    assert w2 == 1.0 and r2 is None  # fail-OPEN


# ───────────────────────── drawdown autopsy ─────────────────────────

def _row(sym, ts, outcome, tp=5.0, sl=3.0, end=0.0):
    return {"symbol": sym, "blocked_at": ts, "outcome": outcome,
            "tp_pct": tp, "sl_pct": sl, "end_pct": end}


def test_autopsy_metric_and_cost_attribution():
    rows = []
    # 10 wins (+5 each) then 20 losses (-3 each): gross peak 50, trough -10 → dd 60
    for i in range(10):
        rows.append(_row("WIN/USD", f"2026-06-01T0{i % 10}:00:00+00:00", "tp_hit"))
    for i in range(20):
        rows.append(_row("LOSE/USD", f"2026-06-02T{i % 24:02d}:30:00+00:00", "sl_hit"))
    out = compute_autopsy(rows, cost_pct=0.30, maker_cost_pct=0.16)
    assert out["n"] == 30
    g = out["scenarios"]["gross_signal_only"]
    t = out["scenarios"]["taker_assumed"]
    m = out["scenarios"]["maker"]
    assert g["max_dd_pct_points"] == pytest.approx(60.0)
    # costs deepen the drawdown: taker > maker > gross
    assert t["max_dd_pct_points"] > m["max_dd_pct_points"] > g["max_dd_pct_points"]
    # dollar translation at fixed $5 sizing
    assert g["max_dd_dollars_at_fixed_size"] == pytest.approx(3.0)
    assert "NOT a compounded account drawdown" in out["verdicts"][0]
    assert any("COST ATTRIBUTION" in v for v in out["verdicts"])
    # LOSE/USD carries 100% of losses → concentration verdict
    top = out["loss_contributions"]["by_symbol"][0]
    assert top["slice"] == "LOSE/USD" and top["share_of_losses"] == 1.0


def test_autopsy_clustering_detects_repeated_moves():
    rows = [_row("PUMP/USD", f"2026-06-01T10:{i:02d}:00+00:00", "sl_hit")
            for i in range(0, 50, 5)]  # 10 obs, 5min apart — same move
    out = compute_autopsy(rows, cost_pct=0.30, maker_cost_pct=0.16)
    assert out["clustering"]["repeat_obs_within_60min_same_symbol"] == 9
    assert out["clustering"]["repeat_share"] == pytest.approx(0.9)
    assert any("CLUSTERING" in v for v in out["verdicts"])


def test_autopsy_empty():
    assert compute_autopsy([], cost_pct=0.3, maker_cost_pct=0.16)["n"] == 0
