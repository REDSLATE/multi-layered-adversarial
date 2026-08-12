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
