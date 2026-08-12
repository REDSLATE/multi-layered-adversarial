"""Regime-conditioned brain expectancy matrix.

Probability-weighted attribution: each resolved outcome carries the
full regime probability vector it was stamped with at intent time.
For brain b, state s:  edge(b,s) = Σ p_s·r / Σ p_s  with effective
sample size Σ p_s. This is exactly the shape the V2 Edge Weight
blend needs (probability-weighted regime edge, not argmax switching).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from shared.outcome_engine import store


def compute_matrix(lane: Optional[str] = None,
                   all_versions: bool = False) -> dict[str, Any]:
    from shared.regime.snapshot import get_cached

    q = ("SELECT brain, lane, executed, actual_return_pct, "
         "theoretical_return_pct, metadata_json FROM rise_signal_outcomes "
         "WHERE metadata_json LIKE '%regime_ctx%'")
    with store._lock:  # noqa: SLF001
        rows = [dict(r) for r in store._get_conn().execute(q).fetchall()]  # noqa: SLF001

    current_versions = {ln: (get_cached(ln) or {}).get("model_version")
                        for ln in ("equity", "crypto")}
    acc: dict[tuple[str, str], dict[int, dict[str, float]]] = {}
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        try:
            ctx = json.loads(row["metadata_json"] or "{}").get("regime_ctx")
        except (json.JSONDecodeError, TypeError):
            continue
        if not ctx or not isinstance(ctx.get("probs"), list):
            continue
        ln = row["lane"]
        if lane and ln != lane:
            continue
        if not all_versions and ctx.get("model_version") != current_versions.get(ln):
            continue
        r = row["actual_return_pct"] if row["executed"] else row["theoretical_return_pct"]
        if r is None:
            continue
        key = (ln, row["brain"])
        cell = acc.setdefault(key, {})
        counts[key] = counts.get(key, 0) + 1
        for s, p in enumerate(ctx["probs"]):
            st = cell.setdefault(s, {"wsum": 0.0, "wret": 0.0})
            st["wsum"] += float(p)
            st["wret"] += float(p) * float(r)

    labels: dict[str, dict[int, str]] = {}
    for ln in ("equity", "crypto"):
        snap = get_cached(ln)
        if snap:
            labels[ln] = {s["state"]: s["label"] for s in snap["states"]}

    out: dict[str, Any] = {"lanes": {}, "stamped_outcomes": len(rows),
                           "all_versions": all_versions,
                           "current_versions": current_versions}
    for (ln, brain), cell in acc.items():
        lane_out = out["lanes"].setdefault(ln, {"brains": {}, "state_labels": labels.get(ln, {})})
        lane_out["brains"][brain] = {
            "n": counts[(ln, brain)],
            "states": {
                str(s): {"eff_n": round(v["wsum"], 2),
                         "edge_pct": (round(v["wret"] / v["wsum"], 4)
                                      if v["wsum"] > 1e-9 else None)}
                for s, v in sorted(cell.items())
            },
        }
    return out
