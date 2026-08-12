"""Feature builders for the Regime Engine (per-lane, fixed definitions).

Doctrine: feature definitions are the contract — training history and
live inference may come from different vendors but MUST be transformed
identically. Any change to a feature definition requires a retrain and
bumps model_version implicitly (trained_at changes).
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

EQUITY_FEATURES = [
    "ret_1d", "rvol_10d", "vol_ratio_10_30", "volume_rel_20d",
    "trend_20d", "qqq_spy_rel_5d", "vix_level", "vix_chg_5d",
]
CRYPTO_FEATURES = [
    "ret_1d", "rvol_10d", "vol_ratio_10_30", "volume_rel_20d",
    "trend_20d", "eth_btc_rel_5d",
]

_WARMUP = 31


def _frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(bars).drop_duplicates("ts").sort_values("ts")
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return df.set_index("date")[["close", "volume"]]


def _core(df: pd.DataFrame, ann: float) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    logc = np.log(df["close"])
    out["ret_1d"] = logc.diff()
    out["rvol_10d"] = out["ret_1d"].rolling(10).std() * np.sqrt(ann)
    rvol30 = out["ret_1d"].rolling(30).std() * np.sqrt(ann)
    out["vol_ratio_10_30"] = out["rvol_10d"] / rvol30.replace(0.0, np.nan) - 1.0
    vol_mean = df["volume"].rolling(20).mean()
    out["volume_rel_20d"] = df["volume"] / vol_mean.replace(0.0, np.nan) - 1.0
    out["trend_20d"] = logc.diff(20)
    out["_logc"] = logc
    return out


def _finalize(feat: pd.DataFrame, names: list[str]) -> Optional[tuple[list[str], np.ndarray]]:
    feat = feat[names].iloc[_WARMUP:].replace([np.inf, -np.inf], np.nan).dropna()
    if len(feat) < 120:
        return None
    return list(feat.index), feat.to_numpy(dtype=float)


def build_equity_features(
    spy: list[dict], qqq: list[dict], vix: list[dict],
) -> Optional[tuple[list[str], np.ndarray, list[str]]]:
    s, q, v = _frame(spy), _frame(qqq), _frame(vix)
    idx = s.index.intersection(q.index).intersection(v.index)
    s, q, v = s.loc[idx], q.loc[idx], v.loc[idx]
    feat = _core(s, 252.0)
    feat["qqq_spy_rel_5d"] = np.log(q["close"]).diff(5) - feat["_logc"].diff(5)
    feat["vix_level"] = np.log(v["close"])
    feat["vix_chg_5d"] = np.log(v["close"]).diff(5)
    packed = _finalize(feat, EQUITY_FEATURES)
    if packed is None:
        return None
    dates, X = packed
    return dates, X, EQUITY_FEATURES


def build_crypto_features(
    btc: list[dict], eth: list[dict],
) -> Optional[tuple[list[str], np.ndarray, list[str]]]:
    b, e = _frame(btc), _frame(eth)
    idx = b.index.intersection(e.index)
    b, e = b.loc[idx], e.loc[idx]
    feat = _core(b, 365.0)
    feat["eth_btc_rel_5d"] = np.log(e["close"]).diff(5) - feat["_logc"].diff(5)
    packed = _finalize(feat, CRYPTO_FEATURES)
    if packed is None:
        return None
    dates, X = packed
    return dates, X, CRYPTO_FEATURES
