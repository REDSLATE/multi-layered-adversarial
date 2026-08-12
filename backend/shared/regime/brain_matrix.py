"""Regime-conditioned brain expectancy matrix — CLUSTER-ADJUSTED.

Operator directive (2026-06): sample thresholds and effective sample
sizes must run on the cluster-adjusted set, not raw observations —
otherwise PUMP×227 makes a regime look mature when it is really one
market move sampled repeatedly.

Clustering:
  * Future data: outcomes stamped with setup_id share a cluster (the
    coalescer already collapses repeats before they become outcomes).
  * Historical data (no setup_id): time-chain heuristic — outcomes for
    the same (lane, symbol, side) whose signal_times are within
    CLUSTER_GAP_MIN of the previous one chain into one cluster,
    ACROSS brains (stack-level, per operator revision).
  * Per-brain weight inside a cluster = 1 / (that brain's observation
    count in the cluster) → each brain counts each independent move
    exactly once.

Probability-weighted edge per brain,state:
    edge(b,s) = Σ w·p_s·r / Σ w·p_s     eff_n(b,s) = Σ w·p_s
Plus per-brain overall_edge and sigma (weighted return std) for the
Regime Edge multiplier.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from shared.outcome_engine import store

CLUSTER_GAP_MIN = 90.0


def _parse_rows(lane_filter: Optional[str], all_versions: bool,
                current_versions: dict) -> list[dict]:
    q = ("SELECT brain, lane, symbol, side, signal_time, executed, "
         "actual_return_pct, theoretical_return_pct, metadata_json "
         "FROM rise_signal_outcomes WHERE metadata_json LIKE '%regime_ctx%'")
    with store._lock:  # noqa: SLF001
        raw = [dict(r) for r in store._get_conn().execute(q).fetchall()]  # noqa: SLF001
    rows = []
    for row in raw:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        ctx = meta.get("regime_ctx")
        if not ctx or not isinstance(ctx.get("probs"), list):
            continue
        if lane_filter and row["lane"] != lane_filter:
            continue
        if not all_versions and \
                ctx.get("model_version") != current_versions.get(row["lane"]):
            continue
        r = (row["actual_return_pct"] if row["executed"]
             else row["theoretical_return_pct"])
        if r is None:
            continue
        rows.append({"brain": row["brain"], "lane": row["lane"],
                     "symbol": row["symbol"], "side": row["side"],
                     "signal_time": row["signal_time"],
                     "ret": float(r), "probs": ctx["probs"],
                     "setup_id": meta.get("setup_id")})
    return rows


def _assign_clusters(rows: list[dict]) -> None:
    """Stamp `cluster` on each row. setup_id wins; else stack-level
    time-chain per (lane, symbol, side)."""
    from datetime import datetime

    def _ts(v):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    chains: dict[tuple, tuple[str, float]] = {}
    seq = 0
    for row in sorted(rows, key=lambda x: str(x["signal_time"])):
        if row["setup_id"]:
            row["cluster"] = f"setup:{row['setup_id']}"
            continue
        key = (row["lane"], row["symbol"], row["side"])
        t = _ts(row["signal_time"])
        prev = chains.get(key)
        if prev and t - prev[1] <= CLUSTER_GAP_MIN * 60.0:
            row["cluster"] = prev[0]
        else:
            seq += 1
            row["cluster"] = f"chain:{seq}"
        chains[key] = (row["cluster"], t)


def _weights(rows: list[dict]) -> None:
    """Per-brain-within-cluster weight = 1/n so each brain counts each
    independent move once."""
    counts: dict[tuple, int] = {}
    for row in rows:
        k = (row["cluster"], row["brain"])
        counts[k] = counts.get(k, 0) + 1
    for row in rows:
        row["w"] = 1.0 / counts[(row["cluster"], row["brain"])]


def compute_matrix(lane: Optional[str] = None,
                   all_versions: bool = False) -> dict[str, Any]:
    from shared.regime.snapshot import get_cached

    current_versions = {ln: (get_cached(ln) or {}).get("model_version")
                        for ln in ("equity", "crypto")}
    rows = _parse_rows(lane, all_versions, current_versions)
    _assign_clusters(rows)
    _weights(rows)

    acc: dict[tuple[str, str], dict[int, dict[str, float]]] = {}
    totals: dict[tuple[str, str], dict[str, float]] = {}
    clusters_seen: dict[tuple[str, str], set] = {}
    for row in rows:
        key = (row["lane"], row["brain"])
        w, r = row["w"], row["ret"]
        tot = totals.setdefault(key, {"n_raw": 0.0, "n_eff": 0.0,
                                      "wr": 0.0, "wr2": 0.0})
        tot["n_raw"] += 1
        tot["n_eff"] += w
        tot["wr"] += w * r
        tot["wr2"] += w * r * r
        clusters_seen.setdefault(key, set()).add(row["cluster"])
        cell = acc.setdefault(key, {})
        for s, p in enumerate(row["probs"]):
            st = cell.setdefault(s, {"wsum": 0.0, "wret": 0.0})
            st["wsum"] += w * float(p)
            st["wret"] += w * float(p) * r

    labels: dict[str, dict[int, str]] = {}
    for ln in ("equity", "crypto"):
        snap = get_cached(ln)
        if snap:
            labels[ln] = {s["state"]: s["label"] for s in snap["states"]}

    out: dict[str, Any] = {"lanes": {}, "stamped_outcomes": len(rows),
                           "all_versions": all_versions,
                           "current_versions": current_versions,
                           "cluster_adjusted": True}
    for (ln, brain), cell in acc.items():
        tot = totals[(ln, brain)]
        n_eff = tot["n_eff"]
        overall = tot["wr"] / n_eff if n_eff > 1e-9 else None
        var = (tot["wr2"] / n_eff - overall * overall) if overall is not None else None
        sigma = math.sqrt(max(var, 0.0)) if var is not None else None
        lane_out = out["lanes"].setdefault(
            ln, {"brains": {}, "state_labels": labels.get(ln, {})})
        lane_out["brains"][brain] = {
            "n_raw": int(tot["n_raw"]),
            "n_eff": round(n_eff, 2),
            "independent_clusters": len(clusters_seen[(ln, brain)]),
            "overall_edge": round(overall, 4) if overall is not None else None,
            "sigma": round(sigma, 4) if sigma is not None else None,
            "states": {
                str(s): {"eff_n": round(v["wsum"], 2),
                         "edge_pct": (round(v["wret"] / v["wsum"], 4)
                                      if v["wsum"] > 1e-9 else None)}
                for s, v in sorted(cell.items())
            },
        }
    return out
