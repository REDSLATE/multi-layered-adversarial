"""Edge Weight / Opportunity Multiplier (2026-06 operator directive).

NOT a gate. A dynamic sizing layer that leans capital toward the
slices (hour-of-day, weekday, symbol) where forward-recorded shadow
outcomes show the strongest after-cost edge, while STILL trading
everything an existing gate would allow:

  strong positive slice + strong live setup → 1.0×
  positive slice                            → 0.75-1.0×
  neutral / insufficient data               → 0.5-0.75×
  historically weak slice                   → 0.25-0.5×

Hard floor (default 0.25) — this layer can NEVER zero a trade or
reject one; only existing live risk/execution conditions do that.
Historical slices never override current market evidence (intent
confidence adjusts the weight) and never become a permanent
allowlist — stats refresh from a rolling window so regime change
re-opens every slice. Outcomes keep collecting across ALL hours and
symbols regardless of weight. Knobs: runtime_flags `_id=edge_weight`.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.edge_weight")

FLAG_ID = "edge_weight"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "floor": 0.25,
    "ceiling": 1.0,
    "neutral": 0.65,
    "min_slice_n": 30,
    "min_symbol_n": 20,
    "window_days": 30,
    "cache_ttl_s": 300,
    "strong_pos_pct": 0.5,
    "weak_neg_pct": -0.5,
    "conf_strong": 0.8,
    "conf_weak": 0.6,
}
_cache: dict[str, tuple[float, dict]] = {}


def reset_for_tests() -> None:
    _cache.clear()


async def get_config() -> dict:
    try:
        from db import db  # noqa: WPS433
        doc = await db["runtime_flags"].find_one(
            {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    return {**DEFAULTS, **doc}


def _hour_bucket(dt: datetime) -> str:
    h = dt.hour // 4 * 4
    return f"{h:02d}-{h + 4:02d} UTC"


async def slice_stats(lane: str, cfg: dict) -> dict:
    """{hour: {...}, weekday: {...}, symbol: {...}} → per-slice
    {n, expectancy_net}. Rolling window, cached; regime change
    re-opens every slice by design."""
    now = time.monotonic()
    cached = _cache.get(lane)
    if cached and now - cached[0] < float(cfg["cache_ttl_s"]):
        return cached[1]
    from db import db  # noqa: WPS433
    from shared.forensics.promotion_gate import (  # noqa: WPS433
        counterfactual_return_pct, get_gate_config,
    )
    from shared.risk_sizer.missed_entries import COLLECTION  # noqa: WPS433
    gate_cfg = await get_gate_config()
    cost = float(gate_cfg["cost_pct"])
    cut = (datetime.now(timezone.utc)
           - timedelta(days=float(cfg["window_days"]))).isoformat()
    rows = await db[COLLECTION].find(
        {"evaluated_at": {"$gte": cut}, "lane": lane,
         "outcome": {"$in": ["tp_hit", "sl_hit", "expired"]}},
        {"_id": 0, "outcome": 1, "tp_pct": 1, "sl_pct": 1, "end_pct": 1,
         "blocked_at": 1, "symbol": 1},
    ).max_time_ms(10000).to_list(5000)
    buckets: dict[str, dict[str, list[float]]] = {
        "hour": {}, "weekday": {}, "symbol": {}}
    for r in rows:
        g = counterfactual_return_pct(r, cost)
        if g is None:
            continue
        try:
            dt = datetime.fromisoformat(
                str(r.get("blocked_at")).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            dt = None
        keys = {"symbol": r.get("symbol")}
        if dt is not None:
            keys["hour"] = _hour_bucket(dt)
            keys["weekday"] = dt.strftime("%a")
        for dim, k in keys.items():
            if k:
                buckets[dim].setdefault(str(k), []).append(g)
    stats = {
        dim: {k: {"n": len(v),
                  "expectancy_net": round(sum(v) / len(v), 4)}
              for k, v in b.items()}
        for dim, b in buckets.items()
    }
    _cache[lane] = (now, stats)
    return stats


def _score(exp: Optional[float], n: int, min_n: int, cfg: dict) -> float:
    if exp is None or n < min_n:
        return float(cfg["neutral"])
    if exp >= float(cfg["strong_pos_pct"]):
        return 1.0
    if exp > 0:
        return 0.85
    if exp > float(cfg["weak_neg_pct"]):
        return 0.5
    return 0.35


async def get_edge_weight(intent: dict) -> tuple[float, Optional[dict]]:
    """(multiplier, receipt). Fail-OPEN to 1.0 — this layer must never
    be the reason a trade doesn't happen."""
    try:
        cfg = await get_config()
        if not cfg.get("enabled", True):
            return 1.0, {"enabled": False, "weight": 1.0}
        lane = (intent.get("lane") or "crypto").lower()
        stats = await slice_stats(lane, cfg)
        now = datetime.now(timezone.utc)
        lookups = (
            ("hour", _hour_bucket(now), int(cfg["min_slice_n"])),
            ("weekday", now.strftime("%a"), int(cfg["min_slice_n"])),
            ("symbol", intent.get("symbol") or "", int(cfg["min_symbol_n"])),
        )
        components = {}
        scores = []
        for dim, key, min_n in lookups:
            s = (stats.get(dim) or {}).get(key)
            exp = s["expectancy_net"] if s else None
            n_obs = s["n"] if s else 0
            sc = _score(exp, n_obs, min_n, cfg)
            components[dim] = {"slice": key, "n": n_obs,
                               "expectancy_net": exp, "score": sc}
            scores.append(sc)
        base = sum(scores) / len(scores)
        conf = intent.get("confidence")
        conf_adj = 0.0
        try:
            if conf is not None:
                c = float(conf)
                if c >= float(cfg["conf_strong"]):
                    conf_adj = 0.15  # current evidence lifts the weight
                elif c < float(cfg["conf_weak"]):
                    conf_adj = -0.10
        except (TypeError, ValueError):
            pass
        weight = max(float(cfg["floor"]),
                     min(float(cfg["ceiling"]), base + conf_adj))
        return weight, {
            "enabled": True,
            "weight": round(weight, 3),
            "base_slice_score": round(base, 3),
            "confidence_adj": conf_adj,
            "confidence": conf,
            "components": components,
            "note": "sizing influence only — never a gate",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("edge_weight fail-open: %s", exc)
        return 1.0, None
