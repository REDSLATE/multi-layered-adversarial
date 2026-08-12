"""Cached RegimeSnapshot worker — hot/cold separation doctrine.

Architecture (operator-pinned, 2026-06):
    market/macro feeds → feature builder → HMM + GMM →
    cached RegimeSnapshot → intent/outcome stamping →
    regime-conditioned Outcome Engine → dashboard

NEVER: intent → wait for regime model → permission to trade.
Intents read the local cache synchronously; a regime-model failure
must never stop execution (get_cached simply returns None).

V1 is ADVISORY ONLY — zero sizing/gating impact. Storage keeps the
full probability vector + model_version + feature_asof so the V2
Edge Weight integration can compute probability-weighted regime edge
(uncertainty pulls the multiplier toward neutral 1.0x, never zero).

Configuration (backend/.env):
    REGIME_ENGINE_ENABLED          default true
    REGIME_REFRESH_INTERVAL_SEC    default 3600
    REGIME_RETRAIN_DAYS            default 7 (walk-forward refit)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("risedual.regime.snapshot")

LANES = ("equity", "crypto")
MODEL_DIR = os.environ.get("REGIME_MODEL_DIR", "/app/backend/data/regime_models")

_cache: dict[str, dict[str, Any]] = {}
_state: dict[str, Any] = {"running": False, "task": None, "last_run": None,
                          "last_error": None, "refresh_count": 0}


def get_cached(lane: str) -> Optional[dict[str, Any]]:
    """Local read, never blocks, never raises. None = advisory absent."""
    return _cache.get(lane)


def regime_ctx_for_intent(lane: Optional[str]) -> Optional[dict[str, Any]]:
    """Compact stamp for shared_intents docs. Full probability vector +
    model_version + feature_asof (reproducible attribution after
    retrains, per operator directive)."""
    snap = _cache.get(lane or "")
    if not snap:
        return None
    return {
        "probs": snap["probs"],
        "top_state": snap["top_state"],
        "top_label": snap["top_label"],
        "entropy": snap["entropy"],
        "model_version": snap["model_version"],
        "feature_asof": snap["feature_asof"],
    }


def _snapshot_file(lane: str) -> Path:
    return Path(MODEL_DIR) / f"snapshot_{lane}.json"


def _load_file_cache() -> None:
    for lane in LANES:
        p = _snapshot_file(lane)
        if p.exists():
            try:
                _cache[lane] = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                pass


async def _fetch_lane_features(lane: str):
    from shared.regime import history
    from shared.regime.features import build_crypto_features, build_equity_features
    if lane == "equity":
        spy, qqq, vix = await asyncio.gather(
            history.yahoo_daily("SPY"), history.yahoo_daily("QQQ"),
            history.yahoo_daily("%5EVIX"))
        if not (spy and qqq and vix):
            return None
        return build_equity_features(spy, qqq, vix)
    btc, eth = await asyncio.gather(
        history.kraken_daily("XBTUSD"), history.kraken_daily("ETHUSD"))
    if not (btc and eth):
        return None
    return build_crypto_features(btc, eth)


def _needs_retrain(bundle: Optional[dict]) -> bool:
    if bundle is None:
        return True
    retrain_days = float(os.environ.get("REGIME_RETRAIN_DAYS") or 7)
    trained = datetime.fromisoformat(bundle["trained_at"])
    age_days = (datetime.now(timezone.utc) - trained).total_seconds() / 86400
    return age_days >= retrain_days


async def refresh_lane(lane: str, force_retrain: bool = False) -> Optional[dict[str, Any]]:
    from shared.regime import hmm_engine
    packed = await _fetch_lane_features(lane)
    if packed is None:
        logger.warning("regime refresh: no features for lane=%s", lane)
        return None
    dates, X, feature_names = packed

    bundle = hmm_engine.load_bundle(lane)
    if force_retrain or _needs_retrain(bundle):
        bundle = await asyncio.to_thread(
            hmm_engine.train_lane, lane, X, dates, feature_names)
    if bundle["feature_names"] != feature_names:
        bundle = await asyncio.to_thread(
            hmm_engine.train_lane, lane, X, dates, feature_names)

    result = await asyncio.to_thread(hmm_engine.infer, bundle, X)
    timeline = await asyncio.to_thread(
        hmm_engine.decode_timeline, bundle, X, dates, 90)
    prev = _cache.get(lane)
    prev_probs = prev["probs"] if prev and prev.get("model_version") == bundle["model_version"] else None
    deltas = ([round(c - p, 4) for c, p in zip(result["probs"], prev_probs)]
              if prev_probs else None)
    labels = {s["state"]: s["label"] for s in bundle["states_meta"]}

    snap = {
        "lane": lane,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_asof": dates[-1],
        "model_version": bundle["model_version"],
        "n_states": bundle["n_states"],
        "probs": result["probs"],
        "top_state": result["top_state"],
        "top_label": labels.get(result["top_state"], f"state_{result['top_state']}"),
        "top_prob": result["top_prob"],
        "entropy": result["entropy"],
        "states": [{"state": s["state"], "label": s["label"],
                    "prob": result["probs"][s["state"]],
                    "occupancy": s["occupancy"]}
                   for s in bundle["states_meta"]],
        "prev_probs": prev_probs,
        "deltas": deltas,
        "transition_row": result["transition_row"],
        "gmm_probs": result["gmm_probs"],
        "agreement": result["agreement"],
        "timeline": timeline,
        "advisory_only": True,
    }
    _cache[lane] = snap
    try:
        p = _snapshot_file(lane)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snap))
    except Exception:  # noqa: BLE001
        pass
    try:
        from db import db
        await db["regime_snapshots"].insert_one(dict(snap))
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime snapshot mongo persist failed: %s", exc)
    return snap


def get_model_meta(lane: str) -> Optional[dict[str, Any]]:
    from shared.regime import hmm_engine
    bundle = hmm_engine.load_bundle(lane)
    if not bundle:
        return None
    return {k: bundle[k] for k in (
        "lane", "model_version", "n_states", "feature_names", "states_meta",
        "bic_report", "stability", "trained_at", "train_start", "train_end",
        "train_samples")}


async def run_now(lane: Optional[str] = None, retrain: bool = False) -> dict[str, Any]:
    lanes = [lane] if lane else list(LANES)
    out = {}
    for ln in lanes:
        try:
            snap = await refresh_lane(ln, force_retrain=retrain)
            out[ln] = {"ok": snap is not None,
                       "model_version": (snap or {}).get("model_version")}
        except Exception as exc:  # noqa: BLE001
            logger.exception("regime run_now failed lane=%s", ln)
            out[ln] = {"ok": False, "error": str(exc)}
    _state["last_run"] = datetime.now(timezone.utc).isoformat()
    return out


async def _worker_loop() -> None:
    interval = float(os.environ.get("REGIME_REFRESH_INTERVAL_SEC") or 3600)
    await asyncio.sleep(10)  # let boot settle
    while _state["running"]:
        try:
            await run_now()
            _state["refresh_count"] += 1
            _state["last_error"] = None
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)
            logger.exception("regime worker cycle failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    if (os.environ.get("REGIME_ENGINE_ENABLED") or "true").lower() != "true":
        logger.info("regime engine worker disabled via env")
        return
    if _state["running"]:
        return
    _load_file_cache()
    _state["running"] = True
    _state["task"] = asyncio.get_event_loop().create_task(_worker_loop())
    logger.info("regime engine worker started")


async def stop_worker() -> None:
    _state["running"] = False
    task = _state.get("task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _state["task"] = None


def worker_status() -> dict[str, Any]:
    return {"running": _state["running"], "last_run": _state["last_run"],
            "refresh_count": _state["refresh_count"],
            "last_error": _state["last_error"]}
