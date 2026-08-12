"""Regime Edge multiplier — V2 promotion of the brain×regime matrix.

Formula (operator-approved 2026-06):
    rel_s   = edge_s − overall_brain_edge      (difference, not ratio)
    m_s     = 1 + rel_s / sigma                (sigma = brain return std)
    raw     = Σ p_s · m_s                      (blend, never argmax switch)
    conf    = min(1, eff_n_weighted / conf_n_min)
    shrunk  = 1 + (raw − 1)·(1 − entropy)·conf
    final   = clamp(shrunk, 0.7, 1.3)

Uncertainty (entropy) AND sparse samples pull toward NEUTRAL 1.0×,
never toward zero. Thresholds run on the CLUSTER-ADJUSTED effective
sample set (brain_matrix), so repeated rescoring of the same move
cannot make a regime look mature.

SHADOW MODE by default: computed, stamped on every entry intent, and
shown on the dashboard — applied to sizing ONLY when
runtime_flags `_id=regime_edge` has armed=true.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("risedual.regime_edge")

FLAG_ID = "regime_edge"
DEFAULTS: dict[str, Any] = {
    "armed": False,
    "clamp_lo": 0.7,
    "clamp_hi": 1.3,
    "state_min_eff_n": 20,
    "brain_min_n": 60,
    "conf_n_min": 20,
    "cache_ttl_s": 300,
}
_matrix_cache: dict[str, Any] = {"ts": 0.0, "matrix": None}


def reset_for_tests() -> None:
    _matrix_cache.update({"ts": 0.0, "matrix": None})


async def get_config() -> dict:
    try:
        from db import db  # noqa: WPS433
        doc = await db["runtime_flags"].find_one(
            {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    return {**DEFAULTS, **doc}


async def _matrix(cfg: dict) -> Optional[dict]:
    now = time.monotonic()
    if _matrix_cache["matrix"] is not None and \
            now - _matrix_cache["ts"] < float(cfg["cache_ttl_s"]):
        return _matrix_cache["matrix"]
    from shared.regime.brain_matrix import compute_matrix  # noqa: WPS433
    m = await asyncio.to_thread(compute_matrix, None, False)
    _matrix_cache.update({"ts": now, "matrix": m})
    return m


def _neutral(reason: str, cfg: dict, **extra: Any) -> tuple[float, dict]:
    return 1.0, {"multiplier": 1.0, "raw_multiplier": 1.0,
                 "armed": bool(cfg.get("armed")), "reason": reason,
                 "eff_n_weighted": 0.0, "entropy": None, "conf": 0.0,
                 **extra}


def _compute(brain_row: dict, snap: dict, cfg: dict,
             lane: str, brain: str) -> tuple[float, dict]:
    overall = brain_row.get("overall_edge")
    sigma = brain_row.get("sigma")
    if overall is None or not sigma or sigma <= 1e-9:
        return _neutral("no_return_dispersion", cfg, lane=lane, brain=brain)
    probs = snap["probs"]
    entropy = float(snap.get("entropy") or 0.0)
    state_mults, raw, effw = {}, 0.0, 0.0
    for s, p in enumerate(probs):
        st = (brain_row.get("states") or {}).get(str(s))
        eff = float(st["eff_n"]) if st else 0.0
        if st and st.get("edge_pct") is not None and \
                eff >= float(cfg["state_min_eff_n"]):
            m_s = 1.0 + (float(st["edge_pct"]) - float(overall)) / float(sigma)
            counted = True
        else:
            m_s, counted = 1.0, False
        state_mults[str(s)] = {"m": round(m_s, 4), "p": p,
                               "eff_n": round(eff, 1), "counted": counted}
        raw += p * m_s
        effw += p * eff
    conf = min(1.0, effw / float(cfg["conf_n_min"])) if cfg["conf_n_min"] else 1.0
    shrunk = 1.0 + (raw - 1.0) * (1.0 - entropy) * conf
    final = max(float(cfg["clamp_lo"]), min(float(cfg["clamp_hi"]), shrunk))
    receipt = {
        "multiplier": round(final, 4),
        "raw_multiplier": round(raw, 4),
        "shrunk_multiplier": round(shrunk, 4),
        "eff_n_weighted": round(effw, 1),
        "entropy": round(entropy, 4),
        "conf": round(conf, 4),
        "armed": bool(cfg.get("armed")),
        "overall_edge": overall, "sigma": sigma,
        "state_multipliers": state_mults,
        "brain_n_eff": brain_row.get("n_eff"),
        "model_version": snap.get("model_version"),
        "lane": lane, "brain": brain,
        "reason": "computed",
        "note": "sizing influence only when armed — never a gate",
    }
    return final, receipt


async def get_regime_edge(intent: dict) -> tuple[float, dict]:
    """(multiplier, receipt). Fail-open to neutral 1.0×."""
    cfg = DEFAULTS
    try:
        cfg = await get_config()
        from shared.regime.snapshot import get_cached  # noqa: WPS433
        lane = (intent.get("lane") or "crypto").lower()
        brain = (intent.get("stack_canonical") or intent.get("stack")
                 or intent.get("brain") or "unknown")
        snap = get_cached(lane)
        if not snap:
            return _neutral("no_regime_snapshot", cfg, lane=lane, brain=brain)
        matrix = await _matrix(cfg)
        brain_row = (((matrix or {}).get("lanes") or {})
                     .get(lane, {}).get("brains", {}).get(brain))
        if not brain_row:
            return _neutral("no_brain_history", cfg, lane=lane, brain=brain)
        if float(brain_row.get("n_eff") or 0) < float(cfg["brain_min_n"]):
            return _neutral("insufficient_brain_samples", cfg, lane=lane,
                            brain=brain, brain_n_eff=brain_row.get("n_eff"))
        return _compute(brain_row, snap, cfg, lane, brain)
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime_edge fail-open: %s", exc)
        return _neutral("error_fail_open", cfg, error=str(exc)[:200])


async def preview_all() -> dict:
    """Shadow multipliers for every brain with history — dashboard."""
    cfg = await get_config()
    from shared.regime.snapshot import LANES, get_cached  # noqa: WPS433
    matrix = await _matrix(cfg)
    out: dict[str, Any] = {"armed": bool(cfg.get("armed")), "config": cfg,
                           "lanes": {}}
    for lane in LANES:
        snap = get_cached(lane)
        lane_brains = ((matrix or {}).get("lanes") or {}).get(lane, {}).get("brains", {})
        rows = {}
        for brain in lane_brains:
            _, receipt = await get_regime_edge({"lane": lane, "stack": brain})
            rows[brain] = receipt
        out["lanes"][lane] = {"snapshot_available": snap is not None,
                              "brains": rows}
    return out
