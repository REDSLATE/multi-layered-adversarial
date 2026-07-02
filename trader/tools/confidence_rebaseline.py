"""Confidence re-baseline harness — SUGGESTS, never applies.

Locked doctrine (2026-07-03, operator directive):

    "Every threshold adjustment must be approved by a human. The
     harness reads N days of receipts, plots the per-brain
     confidence distribution, and SUGGESTS a new floor. It never
     writes anything, never restarts anything, never touches env."

Sign-off shape (locked 2026-07-03, matches merge-rights):
    This harness reads receipts produced by the brains whose
    thresholds it is tuning — same recursive-trust problem as
    merge-rights (a model proposing changes to its own operating
    parameters based on its own output). Therefore:

        harness suggests → optional `--diff` generates advisory
        patch → HUMAN applies by hand. Auto-apply is BANNED.

    The bimodal-refusal check below is the only formal backstop
    inside the harness. A single missed edge case (tri-modal,
    near-threshold bimodal, etc.) would write to prod if
    auto-apply were ever wired. Keeping the human in the loop
    makes that class of miss recoverable.

Runtime dependency (P1a in PRD backlog):
    This tool reads `/app/trader/data/executions.sqlite`. That
    directory is CURRENTLY EPHEMERAL — until the persistent
    volume ships, a pod restart mid-session wipes the history
    this harness needs. Do not tune constants off a session
    that spans a restart until the volume is mounted.

What this tool does
────────────────────
Reads the trader's local SQLite (the truth tape) for a window of
receipts, and per brain (executor) reports:

    * Current threshold        — from BRAIN_DEFAULTS (advisory:
                                  operator overrides live in env
                                  and Mongo, not in this file).
    * Confidence distribution  — p10 / p25 / p50 / p75 / p90 over
                                  all FIRES (chosen brain with a
                                  BUY/SELL verdict).
    * Filled-fires percentiles — same, but restricted to fires
                                  where the broker accepted. This
                                  is the "did the market agree?"
                                  slice.
    * Threshold effectiveness  — what fraction of fires came in
                                  within +5% of the current
                                  threshold? A big number here
                                  means the threshold IS the
                                  operative gate; a tiny number
                                  means it's dead code and the
                                  brain is self-selecting higher.
    * Bimodal flag             — (p90 − p10) > 0.25 → the mean
                                  is lying. Investigate before
                                  suggesting anything.
    * Suggested threshold      — see `_suggest()` below. Prints in
                                  a distinct block so it can never
                                  be mistaken for an applied value.
    * CFQS breakdown           — because the merge-rights view
                                  should be visible while looking
                                  at threshold changes; a brain
                                  that's not merge-eligible today
                                  probably shouldn't have its
                                  threshold loosened.

Usage
─────
    python -m trader.tools.confidence_rebaseline
    python -m trader.tools.confidence_rebaseline --days 7
    python -m trader.tools.confidence_rebaseline --lane equity
    python -m trader.tools.confidence_rebaseline --brain camino
    python -m trader.tools.confidence_rebaseline --db /custom/path.sqlite

Exit codes
──────────
    0 — report emitted, no action taken
    1 — SQLite path unreadable / no receipts in window
    (never non-zero for a "risky suggestion" — the operator judges)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional


# Advisory copy of the DB-backed defaults. If Mongo has an operator
# override for a brain, it wins in production — this table exists
# only so the harness can say "here's what shipped, here's what
# would change" without hitting Atlas.
BRAIN_DEFAULTS_ADVISORY = {
    "camino":    {"doctrine": "trend",           "min_confidence": 0.46},
    "barracuda": {"doctrine": "mean_reversion",  "min_confidence": 0.43},
    "hellcat":   {"doctrine": "breakout",        "min_confidence": 0.48},
    "gto":       {"doctrine": "momentum",        "min_confidence": 0.45},
}


def _default_db_path() -> str:
    return os.environ.get(
        "TRADER_SQLITE_PATH", "/app/trader/data/executions.sqlite",
    )


def _pct(vals: list[float], q: float) -> Optional[float]:
    """Percentile. Returns None if the sample can't produce a boundary."""
    if not vals:
        return None
    if len(vals) < 2:
        return round(vals[0], 4)
    try:
        qs = statistics.quantiles(sorted(vals), n=100)
        idx = min(max(int(q * 100) - 1, 0), 98)
        return round(qs[idx], 4)
    except statistics.StatisticsError:
        return round(sum(vals) / len(vals), 4)


def _load_fires(
    db_path: str, window_days: int,
    lane: Optional[str], brain: Optional[str],
) -> tuple[list[dict], list[dict]]:
    """Return (receipts, executions) — read-only, window-filtered."""
    if not os.path.exists(db_path):
        print(f"ERROR: SQLite path not found: {db_path}", file=sys.stderr)
        return [], []

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat()

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT cycle_id, ts, lane, chosen_json, quote_json "
            "FROM trader_receipts WHERE ts >= ? ORDER BY ts DESC",
            (cutoff,),
        )
        receipts = []
        for r in cur.fetchall():
            chosen = None
            try:
                chosen = json.loads(r["chosen_json"]) if r["chosen_json"] else None
            except json.JSONDecodeError:
                continue
            if not chosen or chosen.get("verdict") not in ("BUY", "SELL"):
                continue
            if lane and (r["lane"] or "").lower() != lane.lower():
                continue
            if brain and chosen.get("brain") != brain:
                continue
            try:
                quote = json.loads(r["quote_json"]) if r["quote_json"] else {}
            except json.JSONDecodeError:
                quote = {}
            receipts.append({
                "cycle_id": r["cycle_id"], "ts": r["ts"],
                "lane": r["lane"], "chosen": chosen, "quote": quote,
            })

        cur2 = conn.execute(
            "SELECT intent_id, ts, lane, brain, ok, exception_type, "
            "notional_usd FROM executions WHERE ts >= ?",
            (cutoff,),
        )
        executions = [dict(row) for row in cur2.fetchall()]
    finally:
        conn.close()

    return receipts, executions


def _suggest(
    *, current: float,
    all_fires_p10: Optional[float],
    filled_fires_p10: Optional[float],
    bimodal: bool,
) -> tuple[Optional[float], str]:
    """Return a SUGGESTED threshold + rationale string.

    Doctrine: the suggestion is the p10 of FILLED fires, floored at
    the current value if the sample says lowering is safe, or raised
    to the p10 of ALL fires if the brain is firing too loose. Never
    returns a value that would flip the sign of the current gate.

    Refuses to suggest anything if the distribution is bimodal —
    the operator has to look at the plot first.
    """
    if bimodal:
        return None, (
            "REFUSED — bimodal distribution (p90 − p10 > 0.25). "
            "The mean is not describing this brain. Inspect the "
            "receipts directly before retuning."
        )
    if filled_fires_p10 is None:
        return None, "insufficient filled sample"

    # A brain whose filled-fires-p10 is well above the current
    # threshold has headroom to raise the floor — lifts noise out.
    # A brain whose filled-fires-p10 sits BELOW the current
    # threshold means the current threshold is filtering fills
    # (bad — investigate before lowering).
    if filled_fires_p10 > current + 0.05:
        return round(filled_fires_p10, 3), (
            f"raise from {current:.2f} to {filled_fires_p10:.3f} — "
            f"filled fires' p10 is comfortably above current gate "
            f"({filled_fires_p10:.3f} vs {current:.2f}), so the "
            f"current floor is not the operative filter."
        )
    if filled_fires_p10 < current - 0.05:
        return None, (
            f"HOLD current {current:.2f} — filled-fires-p10 sits "
            f"BELOW current threshold ({filled_fires_p10:.3f}), which "
            f"means the threshold is chopping fills. Do NOT lower "
            f"until the reason is understood."
        )
    return round(current, 3), (
        f"keep {current:.2f} — distribution matches current gate; "
        f"nothing to move."
    )


def _analyze(
    receipts: list[dict], executions: list[dict],
) -> dict:
    """Bucket receipts by brain and compute the report shape."""
    from collections import defaultdict
    # Build execution index — same intent_id convention the endpoint uses.
    exec_by_intent = {e["intent_id"]: e for e in executions}

    per_brain: dict[str, dict] = defaultdict(lambda: {
        "all_confidences": [], "filled_confidences": [],
        "fires": 0, "fills": 0, "broker_errors": 0,
        "quote_ages": [], "spreads": [],
    })

    for r in receipts:
        chosen = r["chosen"]
        brain = chosen.get("brain")
        if not brain:
            continue
        conf = chosen.get("confidence")
        if not isinstance(conf, (int, float)):
            continue
        b = per_brain[brain]
        b["fires"] += 1
        b["all_confidences"].append(float(conf))
        q = r.get("quote") or {}
        ag = q.get("quote_age_ms")
        if isinstance(ag, (int, float)):
            b["quote_ages"].append(float(ag))
        sp = q.get("spread_bps")
        if isinstance(sp, (int, float)):
            b["spreads"].append(float(sp))
        intent_id = f"trader-{(r['cycle_id'] or '')[:16]}-{r['lane']}"
        ex = exec_by_intent.get(intent_id)
        if ex:
            if ex.get("ok"):
                b["fills"] += 1
                b["filled_confidences"].append(float(conf))
            if ex.get("exception_type"):
                b["broker_errors"] += 1

    # Import CFQS lazily so this tool is standalone-runnable.
    sys.path.insert(0, "/app")
    from trader.merge_rights import compute_cfqs

    # Lane-median spread across brains for CFQS
    all_spread_avgs = []
    for b in per_brain.values():
        if b["spreads"]:
            all_spread_avgs.append(sum(b["spreads"]) / len(b["spreads"]))
    lane_median = (
        round(statistics.median(all_spread_avgs), 4)
        if all_spread_avgs else None
    )

    report = {"lane_median_spread_bps": lane_median, "brains": []}
    for brain, b in sorted(per_brain.items(), key=lambda kv: -kv[1]["fires"]):
        p10 = _pct(b["all_confidences"], 0.10)
        p25 = _pct(b["all_confidences"], 0.25)
        p50 = _pct(b["all_confidences"], 0.50)
        p75 = _pct(b["all_confidences"], 0.75)
        p90 = _pct(b["all_confidences"], 0.90)
        bimodal = (
            p10 is not None and p90 is not None and (p90 - p10) > 0.25
        )
        current = (
            BRAIN_DEFAULTS_ADVISORY.get(brain, {}).get("min_confidence", 0.50)
        )
        # Fires-near-threshold: what fraction sat within +0.05 of gate?
        near = sum(
            1 for c in b["all_confidences"]
            if current <= c <= current + 0.05
        )
        near_pct = round(near / max(b["fires"], 1) * 100, 1)

        filled_p10 = _pct(b["filled_confidences"], 0.10)
        suggested, rationale = _suggest(
            current=current,
            all_fires_p10=p10,
            filled_fires_p10=filled_p10,
            bimodal=bimodal,
        )

        avg_spread = (
            round(sum(b["spreads"]) / len(b["spreads"]), 4)
            if b["spreads"] else None
        )
        cfqs = compute_cfqs(
            fires=b["fires"], fills=b["fills"],
            broker_errors=b["broker_errors"],
            confidence_n=len(b["all_confidences"]),
            p50_quote_age_ms=_pct(b["quote_ages"], 0.50),
            avg_spread_bps=avg_spread,
            lane_median_spread_bps=lane_median,
            confidence_p10=p10, confidence_p90=p90,
        )

        report["brains"].append({
            "brain": brain,
            "current_threshold": current,
            "fires": b["fires"],
            "fills": b["fills"],
            "fill_rate_pct": round(
                b["fills"] / max(b["fires"], 1) * 100, 1
            ),
            "confidence": {
                "p10": p10, "p25": p25, "p50": p50, "p75": p75, "p90": p90,
                "n": len(b["all_confidences"]),
                "bimodal": bimodal,
            },
            "filled_confidence_p10": filled_p10,
            "fires_within_5pct_of_threshold": near,
            "fires_within_5pct_of_threshold_pct": near_pct,
            "suggested_threshold": suggested,
            "rationale": rationale,
            "cfqs": cfqs.to_dict(),
        })

    return report


def _print_report(report: dict, window_days: int) -> None:
    banner = "═" * 72
    print(banner)
    print(f"  CONFIDENCE RE-BASELINE — {window_days}d window")
    print(f"  {datetime.now(timezone.utc).isoformat()}")
    print(f"  lane median spread: {report['lane_median_spread_bps']} bps")
    print(banner)
    if not report["brains"]:
        print("  No fires found in window. Nothing to re-baseline.")
        print(banner)
        return
    for b in report["brains"]:
        print()
        print(f"  ── {b['brain'].upper()} ──")
        print(f"     current threshold : {b['current_threshold']:.2f}")
        print(
            f"     fires={b['fires']}  fills={b['fills']}  "
            f"fill_rate={b['fill_rate_pct']}%"
        )
        c = b["confidence"]
        print(
            f"     confidence:  p10={c['p10']}  p25={c['p25']}  "
            f"p50={c['p50']}  p75={c['p75']}  p90={c['p90']}  n={c['n']}"
        )
        if c["bimodal"]:
            print("     ⚠ BIMODAL — p90 − p10 > 0.25. Investigate.")
        print(
            f"     filled-fires p10 : {b['filled_confidence_p10']}"
        )
        print(
            f"     fires within +5% of gate: "
            f"{b['fires_within_5pct_of_threshold']} "
            f"({b['fires_within_5pct_of_threshold_pct']}%)"
        )
        print(f"     CFQS score       : {b['cfqs']['score']}")
        print(f"     merge_eligible   : {b['cfqs']['merge_eligible']}")
        print()
        print("     ┌─ SUGGESTED (advisory, never auto-applied) ──────")
        print(f"     │  proposed threshold : {b['suggested_threshold']}")
        print(f"     │  rationale          : {b['rationale']}")
        print("     └──────────────────────────────────────────────────")
    print()
    print(banner)
    print(
        "  Nothing has been written. To apply a suggested threshold, "
        "\n  set TRADER_CONFIDENCE_THRESHOLD or update the brain_registry "
        "\n  Mongo doc — operator decision, not this tool's."
    )
    print(banner)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Suggest per-brain confidence thresholds from live receipts.",
    )
    p.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    p.add_argument("--lane", type=str, default=None, help="equity | crypto")
    p.add_argument("--brain", type=str, default=None, help="filter to one brain")
    p.add_argument("--db", type=str, default=_default_db_path(),
                   help="path to executions.sqlite")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of a report")
    args = p.parse_args()

    receipts, executions = _load_fires(
        args.db, args.days, args.lane, args.brain,
    )
    if not receipts:
        print(
            f"No fires in the last {args.days}d "
            f"(lane={args.lane or 'any'}, brain={args.brain or 'any'}). "
            f"Nothing to re-baseline.",
            file=sys.stderr,
        )
        return 1

    report = _analyze(receipts, executions)
    report["window_days"] = args.days
    report["lane_filter"] = args.lane
    report["brain_filter"] = args.brain

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
