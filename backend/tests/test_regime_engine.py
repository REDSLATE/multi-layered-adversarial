"""Regime Engine unit tests — features, training constraints, inference."""
import numpy as np
import pytest

from shared.regime.features import (
    CRYPTO_FEATURES, EQUITY_FEATURES, build_crypto_features,
    build_equity_features,
)
from shared.regime import hmm_engine


def _synth_bars(n=400, seed=0, base=100.0):
    rng = np.random.default_rng(seed)
    # two alternating vol regimes so the HMM has something to find
    vols = np.where((np.arange(n) // 60) % 2 == 0, 0.008, 0.03)
    rets = rng.normal(0.0004, vols)
    close = base * np.exp(np.cumsum(rets))
    vol = rng.uniform(1e6, 5e6, n) * (1 + vols * 20)
    ts = 1_600_000_000 + np.arange(n) * 86400
    return [{"ts": int(t), "close": float(c), "volume": float(v)}
            for t, c, v in zip(ts, close, vol)]


def test_equity_features_shape():
    packed = build_equity_features(_synth_bars(seed=1), _synth_bars(seed=2),
                                   _synth_bars(seed=3, base=20.0))
    assert packed is not None
    dates, X, names = packed
    assert names == EQUITY_FEATURES
    assert X.shape[1] == len(EQUITY_FEATURES)
    assert len(dates) == X.shape[0]
    assert np.isfinite(X).all()


def test_crypto_features_shape():
    packed = build_crypto_features(_synth_bars(seed=4), _synth_bars(seed=5))
    assert packed is not None
    dates, X, names = packed
    assert names == CRYPTO_FEATURES
    assert np.isfinite(X).all()


def test_train_and_infer(tmp_path, monkeypatch):
    monkeypatch.setattr(hmm_engine, "MODEL_DIR", str(tmp_path))
    packed = build_crypto_features(_synth_bars(seed=6), _synth_bars(seed=7))
    dates, X, names = packed
    bundle = hmm_engine.train_lane("crypto", X, dates, names)

    # BIC report covers 3-6 states, chosen model respects occupancy
    assert {r["n_states"] for r in bundle["bic_report"] if "bic" in r} <= {3, 4, 5, 6}
    assert bundle["n_states"] in (3, 4, 5, 6)
    assert bundle["model_version"].startswith("crypto-n")
    occ = [s["occupancy"] for s in bundle["states_meta"]]
    assert min(occ) >= 0.05 or not any(r.get("valid") for r in bundle["bic_report"])

    # labels are unique per state (post-hoc, never pre-assigned)
    labels = [s["label"] for s in bundle["states_meta"]]
    assert len(set(labels)) == len(labels)

    result = hmm_engine.infer(bundle, X)
    probs = np.array(result["probs"])
    assert probs.shape == (bundle["n_states"],)
    assert probs.sum() == pytest.approx(1.0, abs=0.01)
    assert 0.0 <= result["entropy"] <= 1.0
    assert len(result["gmm_probs"]) == bundle["n_states"]
    assert "argmax_match" in result["agreement"]

    # reload from disk keeps the same version
    loaded = hmm_engine.load_bundle("crypto")
    assert loaded["model_version"] == bundle["model_version"]


def test_regime_ctx_shape():
    from shared.regime import snapshot as snap_mod
    snap_mod._cache["equity"] = {
        "probs": [0.6, 0.1, 0.25, 0.05], "top_state": 0,
        "top_label": "low_vol_trend_up", "entropy": 0.42,
        "model_version": "equity-n4-x", "feature_asof": "2026-06-01",
    }
    ctx = snap_mod.regime_ctx_for_intent("equity")
    assert ctx["probs"] == [0.6, 0.1, 0.25, 0.05]
    assert ctx["model_version"] == "equity-n4-x"
    assert ctx["feature_asof"] == "2026-06-01"
    assert snap_mod.regime_ctx_for_intent("nope") is None
    snap_mod._cache.pop("equity", None)


def test_decode_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(hmm_engine, "MODEL_DIR", str(tmp_path))
    dates, X, names = build_crypto_features(_synth_bars(seed=8), _synth_bars(seed=9))
    bundle = hmm_engine.train_lane("crypto", X, dates, names)
    tl = hmm_engine.decode_timeline(bundle, X, dates, days=90)
    assert len(tl) == 90
    assert tl[-1]["date"] == dates[-1]
    assert all(0 <= t["state"] < bundle["n_states"] for t in tl)
    assert all(t["label"] for t in tl)


def test_regime_edge_math():
    """Formula pin: blend, uncertainty shrink toward 1.0, clamp."""
    from shared.regime.regime_edge import DEFAULTS, _compute, _neutral

    cfg = dict(DEFAULTS)
    snap = {"probs": [0.6, 0.1, 0.25, 0.05], "entropy": 0.3,
            "model_version": "test-v"}
    brain_row = {
        "overall_edge": 0.5, "sigma": 2.0, "n_eff": 100,
        "states": {
            "0": {"eff_n": 40, "edge_pct": 2.5},   # +1σ state
            "1": {"eff_n": 30, "edge_pct": -1.5},  # -1σ state
            "2": {"eff_n": 5, "edge_pct": 9.0},    # below threshold → neutral
            "3": {"eff_n": 25, "edge_pct": 0.5},   # exactly overall → 1.0
        },
    }
    mult, r = _compute(brain_row, snap, cfg, "crypto", "gto")
    # raw = .6*2.0 + .1*0.0... m0=1+(2.5-.5)/2=2.0, m1=1+(-1.5-.5)/2=0.0,
    # m2=1.0 (threshold), m3=1.0 → raw = .6*2 + .1*0 + .25*1 + .05*1 = 1.5
    assert r["raw_multiplier"] == pytest.approx(1.5, abs=1e-6)
    # eff_n_weighted = .6*40+.1*30+.25*5+.05*25 = 29.5 → conf=1.0
    assert r["conf"] == 1.0
    # shrunk = 1 + 0.5*(1-0.3)*1 = 1.35 → clamped to 1.3
    assert mult == 1.3
    assert r["multiplier"] == 1.3
    assert not r["armed"]

    # sparse samples pull toward neutral, never zero
    sparse = dict(brain_row, states={
        "0": {"eff_n": 21, "edge_pct": 2.5},
        "1": {"eff_n": 0, "edge_pct": None},
        "2": {"eff_n": 0, "edge_pct": None},
        "3": {"eff_n": 0, "edge_pct": None}})
    m2, r2 = _compute(sparse, snap, cfg, "crypto", "gto")
    assert 1.0 < m2 < 1.3
    assert r2["conf"] < 1.0

    # zero dispersion → neutral
    m3, r3 = _neutral("no_return_dispersion", cfg)
    assert m3 == 1.0 and r3["multiplier"] == 1.0


def test_cluster_adjustment():
    """PUMP×227 pin: repeats of one move collapse to weight 1 per brain."""
    from shared.regime.brain_matrix import _assign_clusters, _weights

    base = "2026-06-01T10:{m:02d}:00+00:00"
    rows = []
    for i in range(10):  # gto rescored the same move 10× in 20 min
        rows.append({"lane": "crypto", "brain": "gto", "symbol": "PUMP/USD",
                     "side": "buy", "signal_time": base.format(m=i * 2),
                     "ret": 1.0, "probs": [1.0], "setup_id": None})
    rows.append({"lane": "crypto", "brain": "hellcat", "symbol": "PUMP/USD",
                 "side": "buy", "signal_time": base.format(m=5),
                 "ret": 1.0, "probs": [1.0], "setup_id": None})
    rows.append({"lane": "crypto", "brain": "gto", "symbol": "PUMP/USD",
                 "side": "buy", "signal_time": "2026-06-01T15:00:00+00:00",
                 "ret": 1.0, "probs": [1.0], "setup_id": None})  # new move
    _assign_clusters(rows)
    _weights(rows)
    gto_eff = sum(r["w"] for r in rows if r["brain"] == "gto")
    hc_eff = sum(r["w"] for r in rows if r["brain"] == "hellcat")
    assert gto_eff == pytest.approx(2.0)   # 2 independent moves, not 11
    assert hc_eff == pytest.approx(1.0)
    # cross-brain: hellcat shares gto's cluster (stack-level chain)
    hc_cluster = next(r["cluster"] for r in rows if r["brain"] == "hellcat")
    assert hc_cluster == rows[0]["cluster"]
