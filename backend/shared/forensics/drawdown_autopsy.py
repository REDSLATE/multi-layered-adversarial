"""Drawdown Autopsy (2026-06 operator directive).

Validates what `max_drawdown_per_100_obs` actually measures before
anyone engineers around it. The gate's drawdown is the max peak-to-
trough dip of the SUM of per-observation counterfactual % returns —
percentage POINTS across fixed-size shadow trades, NOT a compounded
account drawdown. This module decomposes it: cost scenarios (gross /
taker / maker), the exact drawdown window, loss contribution by
symbol / hour / weekday, and repeated-observation clustering (the
same move counted many times inflates the curve).

Pure computation — the route in execution_mode_admin feeds it rows.
Diagnostic only: nothing here gates or blocks trades.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _gross(row: dict) -> Optional[float]:
    from shared.forensics.promotion_gate import counterfactual_return_pct
    return counterfactual_return_pct(row, 0.0)


def _hour_bucket(ts: str) -> Optional[str]:
    if not ts or len(ts) < 13:
        return None
    h = int(ts[11:13]) // 4 * 4
    return f"{h:02d}-{h + 4:02d} UTC"


def _weekday(ts: str) -> Optional[str]:
    try:
        return datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).strftime("%a")
    except Exception:  # noqa: BLE001
        return None


def _dd_curve(grosses: list[float], cost: float) -> dict:
    cum = peak = dd = 0.0
    peak_i = dd_start = dd_end = 0
    for i, g in enumerate(grosses):
        cum += g - cost
        if cum > peak:
            peak, peak_i = cum, i
        if peak - cum > dd:
            dd, dd_start, dd_end = peak - cum, peak_i, i
    return {"max_dd_pct_points": round(dd, 2),
            "cumulative_pct_points": round(cum, 2),
            "dd_start_idx": dd_start, "dd_end_idx": dd_end}


def compute_autopsy(rows: list[dict], *, cost_pct: float,
                    maker_cost_pct: float,
                    notional_usd: float = 5.0) -> dict:
    """`rows` must be chronological (blocked_at asc) scored ledger rows."""
    scored = [(r, g) for r in rows if (g := _gross(r)) is not None]
    n = len(scored)
    if n == 0:
        return {"n": 0, "verdicts": ["no scored observations"]}
    grosses = [g for _, g in scored]

    scenarios = {}
    for label, cost in (("gross_signal_only", 0.0),
                        ("taker_assumed", cost_pct),
                        ("maker", maker_cost_pct)):
        c = _dd_curve(grosses, cost)
        dd_per_100 = c["max_dd_pct_points"] * 100.0 / max(n, 100)
        scenarios[label] = {
            "cost_pct": cost,
            **c,
            "dd_per_100_obs": round(dd_per_100, 3),
            "max_dd_dollars_at_fixed_size": round(
                c["max_dd_pct_points"] / 100.0 * notional_usd, 2),
            "expectancy_pct": round(sum(grosses) / n - cost, 4),
        }

    taker = scenarios["taker_assumed"]
    window = scored[taker["dd_start_idx"]:taker["dd_end_idx"] + 1]
    win_rows = [r for r, _ in window]
    window_summary = {
        "n_obs": len(window),
        "from": (win_rows[0].get("blocked_at") if win_rows else None),
        "to": (win_rows[-1].get("blocked_at") if win_rows else None),
    }

    def _loss_contrib(pairs, key_fn, top=8):
        buckets: dict[str, float] = {}
        counts: dict[str, int] = {}
        total = 0.0
        for r, g in pairs:
            net = g - cost_pct
            if net >= 0:
                continue
            k = key_fn(r)
            if k is None:
                continue
            buckets[k] = buckets.get(k, 0.0) + net
            counts[k] = counts.get(k, 0) + 1
            total += net
        out = sorted(buckets.items(), key=lambda kv: kv[1])[:top]
        return [{"slice": k, "loss_pct_points": round(v, 2),
                 "n_losses": counts[k],
                 "share_of_losses": round(v / total, 3) if total else None}
                for k, v in out]

    contributions = {
        "by_symbol": _loss_contrib(scored, lambda r: r.get("symbol")),
        "by_hour_utc": _loss_contrib(
            scored, lambda r: _hour_bucket(str(r.get("blocked_at") or ""))),
        "by_weekday": _loss_contrib(
            scored, lambda r: _weekday(str(r.get("blocked_at") or ""))),
        "in_dd_window_by_symbol": _loss_contrib(
            window, lambda r: r.get("symbol"), top=5),
    }

    # Repeated-observation clustering: same symbol blocked again within
    # 60 min — the same underlying move scored multiple times.
    last_seen: dict[str, datetime] = {}
    repeats = 0
    sym_counts: dict[str, int] = {}
    for r, _ in scored:
        sym = r.get("symbol") or "?"
        sym_counts[sym] = sym_counts.get(sym, 0) + 1
        try:
            ts = datetime.fromisoformat(
                str(r.get("blocked_at")).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        prev = last_seen.get(sym)
        if prev is not None and (ts - prev).total_seconds() <= 3600:
            repeats += 1
        last_seen[sym] = ts
    clustering = {
        "repeat_obs_within_60min_same_symbol": repeats,
        "repeat_share": round(repeats / n, 3),
        "top_symbols_by_obs": sorted(
            ({"symbol": k, "n": v} for k, v in sym_counts.items()),
            key=lambda d: d["n"], reverse=True)[:8],
        "distinct_symbols": len(sym_counts),
    }

    verdicts: list[Any] = [
        ("METRIC: sum-of-percentage-POINTS drawdown across fixed-size "
         f"counterfactual trades — NOT a compounded account drawdown. Raw "
         f"{taker['max_dd_pct_points']} pct-points over {n} obs ≈ "
         f"${taker['max_dd_dollars_at_fixed_size']} at fixed "
         f"${notional_usd:g} sizing.")]
    g_dd = scenarios["gross_signal_only"]["dd_per_100_obs"]
    t_dd = taker["dd_per_100_obs"]
    m_dd = scenarios["maker"]["dd_per_100_obs"]
    if t_dd > 0:
        cost_share = (t_dd - g_dd) / t_dd
        verdicts.append(
            f"COST ATTRIBUTION: drawdown/100obs is {g_dd} on gross signal, "
            f"{m_dd} at maker costs, {t_dd} at assumed taker costs — "
            f"{round(cost_share * 100)}% of the drawdown is execution cost, "
            f"not signal.")
    top_sym = contributions["by_symbol"][:1]
    if top_sym and (top_sym[0]["share_of_losses"] or 0) >= 0.2:
        verdicts.append(
            f"CONCENTRATION: {top_sym[0]['slice']} alone contributes "
            f"{round((top_sym[0]['share_of_losses'] or 0) * 100)}% of all "
            "losses.")
    if clustering["repeat_share"] >= 0.3:
        verdicts.append(
            f"CLUSTERING: {round(clustering['repeat_share'] * 100)}% of "
            "observations are repeats of the same symbol within 60min — "
            "the same move is scored many times, inflating both the "
            "observation count and the drawdown curve.")

    return {
        "n": n,
        "scenarios": scenarios,
        "dd_window": window_summary,
        "loss_contributions": contributions,
        "clustering": clustering,
        "verdicts": verdicts,
    }
