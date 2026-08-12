"""HMM + GMM regime models — train, BIC-constrained selection, infer.

Operator directives (2026-06):
  * Unsupervised state discovery — NO pre-labeled BULL/BEAR. States
    are State 0..N, characterized AFTER training from observed
    properties (vol, trend, volume).
  * BIC auto-selection over 3-6 states with two safeguards: minimum
    state occupancy, and instability rejection (tiny states that
    appear/disappear across refits). BIC/AIC reported per candidate.
  * GMM is the diagnostic benchmark, mapped to HMM states by nearest
    mean, so HMM-vs-GMM agreement is a first-class output.
  * Full probability vector is the product — never just argmax.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
from hmmlearn.hmm import GaussianHMM
from sklearn.mixture import GaussianMixture

logger = logging.getLogger("risedual.regime.hmm")

MODEL_DIR = os.environ.get("REGIME_MODEL_DIR", "/app/backend/data/regime_models")
STATE_RANGE = range(3, 7)
MIN_OCC_FRAC = 0.05
MIN_OCC_ABS = 15


def _bundle_path(lane: str) -> Path:
    return Path(MODEL_DIR) / f"{lane}_regime.joblib"


def _characterize(means_z: np.ndarray, scaler_mean: np.ndarray,
                  scaler_std: np.ndarray, feature_names: list[str],
                  occupancy: np.ndarray) -> list[dict[str, Any]]:
    """Post-hoc state labels derived from state properties, never
    hard-coded before training."""
    idx = {n: i for i, n in enumerate(feature_names)}
    out = []
    for s in range(means_z.shape[0]):
        z = means_z[s]
        raw = z * scaler_std + scaler_mean
        vol_z = z[idx["rvol_10d"]]
        trend_z = z[idx["trend_20d"]]
        volu_z = z[idx["volume_rel_20d"]]
        vol_tag = "high_vol" if vol_z > 0.5 else ("low_vol" if vol_z < -0.5 else "mid_vol")
        trend_tag = "trend_up" if trend_z > 0.4 else ("trend_down" if trend_z < -0.4 else "flat")
        label = f"{vol_tag}_{trend_tag}"
        if vol_tag == "mid_vol" and trend_tag == "flat":
            label = "chop" if abs(z[idx["ret_1d"]]) < 0.3 else "mixed"
        if any(o["label"] == label for o in out) or label in (
                o["label"].rsplit("_s", 1)[0] for o in out):
            label = f"{label}_s{s}"
        out.append({
            "state": s, "label": label,
            "occupancy": round(float(occupancy[s]), 4),
            "elevated_volume": bool(volu_z > 0.5),
            "mean_features": {n: round(float(raw[i]), 5) for n, i in idx.items()},
            "mean_z": {n: round(float(z[i]), 3) for n, i in idx.items()},
        })
    return out


def train_lane(lane: str, X: np.ndarray, dates: list[str],
               feature_names: list[str]) -> dict[str, Any]:
    """Fit HMM candidates 3-6 states, pick lowest BIC among candidates
    that pass occupancy constraints; fit a same-N GMM benchmark; save
    bundle (model + scaler + characterization + audit report)."""
    scaler_mean = X.mean(axis=0)
    scaler_std = X.std(axis=0)
    scaler_std[scaler_std < 1e-9] = 1.0
    Z = (X - scaler_mean) / scaler_std
    T = len(Z)
    min_occ = max(MIN_OCC_FRAC, MIN_OCC_ABS / T)

    report, fits = [], {}
    for n in STATE_RANGE:
        try:
            hmm = GaussianHMM(n_components=n, covariance_type="diag",
                              n_iter=300, random_state=42)
            hmm.fit(Z)
            states = hmm.predict(Z)
            occ = np.bincount(states, minlength=n) / T
            valid = bool(occ.min() >= min_occ)
            entry = {"n_states": n, "bic": round(float(hmm.bic(Z)), 1),
                     "aic": round(float(hmm.aic(Z)), 1),
                     "min_occupancy": round(float(occ.min()), 4),
                     "occupancy": [round(float(o), 4) for o in occ],
                     "valid": valid,
                     "converged": bool(hmm.monitor_.converged)}
            fits[n] = (hmm, occ)
        except Exception as exc:  # noqa: BLE001
            entry = {"n_states": n, "error": str(exc), "valid": False}
        report.append(entry)

    valid_entries = [r for r in report if r.get("valid")]
    pool = valid_entries or [r for r in report if "bic" in r]
    if not pool:
        raise RuntimeError(f"regime train failed for lane={lane}: no fit converged")
    chosen = min(pool, key=lambda r: r["bic"])
    n = chosen["n_states"]
    hmm, occ = fits[n]

    gmm = GaussianMixture(n_components=n, covariance_type="diag",
                          random_state=42, n_init=3).fit(Z)
    # map each GMM component to the nearest HMM state mean
    gmm_map = [int(np.argmin(np.linalg.norm(hmm.means_ - gmm.means_[j], axis=1)))
               for j in range(n)]

    states_meta = _characterize(hmm.means_, scaler_mean, scaler_std,
                                feature_names, occ)
    trained_at = datetime.now(timezone.utc)
    model_version = f"{lane}-n{n}-{trained_at.strftime('%Y%m%d%H%M')}"

    # instability note vs previous refit (report-only in V1)
    stability = {"prev_model_version": None, "n_states_changed": None,
                 "label_set_changed": None}
    prev = load_bundle(lane)
    if prev:
        stability = {
            "prev_model_version": prev["model_version"],
            "n_states_changed": prev["n_states"] != n,
            "label_set_changed": sorted(s["label"] for s in prev["states_meta"])
            != sorted(s["label"] for s in states_meta),
        }

    bundle = {
        "lane": lane, "model_version": model_version, "n_states": n,
        "hmm": hmm, "gmm": gmm, "gmm_map": gmm_map,
        "scaler_mean": scaler_mean, "scaler_std": scaler_std,
        "feature_names": feature_names, "states_meta": states_meta,
        "bic_report": report, "stability": stability,
        "trained_at": trained_at.isoformat(),
        "train_start": dates[0], "train_end": dates[-1],
        "train_samples": T,
    }
    path = _bundle_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)
    logger.info("regime model trained lane=%s version=%s n=%s bic=%s",
                lane, model_version, n, chosen["bic"])
    return bundle


def load_bundle(lane: str) -> Optional[dict[str, Any]]:
    path = _bundle_path(lane)
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime bundle load failed lane=%s: %s", lane, exc)
        return None


def decode_timeline(bundle: dict[str, Any], X: np.ndarray,
                    dates: list[str], days: int = 90) -> list[dict[str, Any]]:
    """Posterior-decoded state per day for the trailing window —
    powers the dashboard regime ribbon."""
    Z = (X - bundle["scaler_mean"]) / bundle["scaler_std"]
    post = bundle["hmm"].predict_proba(Z)
    labels = {s["state"]: s["label"] for s in bundle["states_meta"]}
    take = min(days, len(dates))
    out = []
    for i in range(len(dates) - take, len(dates)):
        s = int(np.argmax(post[i]))
        out.append({"date": dates[i], "state": s,
                    "label": labels.get(s, f"state_{s}"),
                    "prob": round(float(post[i][s]), 3)})
    return out


def infer(bundle: dict[str, Any], X: np.ndarray) -> dict[str, Any]:
    """Posterior probability vector for the LAST observation, plus
    transition diagnostics and GMM benchmark agreement."""
    Z = (X - bundle["scaler_mean"]) / bundle["scaler_std"]
    hmm: GaussianHMM = bundle["hmm"]
    n = bundle["n_states"]
    probs = hmm.predict_proba(Z)[-1]
    top = int(np.argmax(probs))
    gmm_raw = bundle["gmm"].predict_proba(Z[-1:])[0]
    gmm_probs = np.zeros(n)
    for j, i in enumerate(bundle["gmm_map"]):
        gmm_probs[i] += gmm_raw[j]
    overlap = float(np.minimum(probs, gmm_probs).sum())
    entropy = float(-(probs * np.log(np.clip(probs, 1e-12, 1))).sum() / np.log(n))
    return {
        "probs": [round(float(p), 4) for p in probs],
        "top_state": top,
        "top_prob": round(float(probs[top]), 4),
        "transition_row": [round(float(p), 4) for p in hmm.transmat_[top]],
        "gmm_probs": [round(float(p), 4) for p in gmm_probs],
        "agreement": {"argmax_match": bool(int(np.argmax(gmm_probs)) == top),
                      "overlap": round(overlap, 4)},
        "entropy": round(entropy, 4),
    }
