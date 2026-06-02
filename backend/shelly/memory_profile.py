"""Brain MEMORY.md profile generator.

Renders one LocalShelly's state as a human-readable markdown document.
Operator opens it to see at a glance what a single brain "remembers"
right now: most-recent events, win/loss skew, top symbols, top features.

Authority: pure read. Never mutates a brain's memory or any
collection. Output is text.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from shelly.contracts import AUTHORITY_MEMORY_REASONING_ONLY
from shelly.local_shelly import LocalShelly


MEMORY_PROFILE_RECENT_LIMIT = 50


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_brain_memory_md(brain: str, *,
                           recent_limit: int = MEMORY_PROFILE_RECENT_LIMIT) -> str:
    """Build a MEMORY.md-style markdown profile for one brain.

    Returns the full markdown string. Safe to call repeatedly — no
    side effects."""
    ls = LocalShelly(brain)
    total = ls.memories.count_documents({})

    # Most recent events first.
    recent = list(
        ls.memories.find(
            {}, {"_id": 0, "embedding": 0, "embedding_provider": 0},
        )
        .sort("created_at", -1)
        .limit(recent_limit)
    )

    n_resolved = sum(1 for r in recent if r.get("outcome"))
    pnls = []
    for r in recent:
        o = r.get("outcome") or {}
        pct = o.get("pnl_pct")
        if pct is None:
            continue
        try:
            pnls.append(float(pct))
        except (TypeError, ValueError):
            continue
    wins = sum(1 for p in pnls if p > 0.001)
    losses = sum(1 for p in pnls if p < -0.001)
    flat = len(pnls) - wins - losses
    avg_pnl = (sum(pnls) / len(pnls)) if pnls else None

    symbols = Counter(r.get("symbol") for r in recent if r.get("symbol"))
    directions = Counter(r.get("direction") for r in recent if r.get("direction"))

    feature_counter: Counter[str] = Counter()
    for r in recent:
        for k, v in (r.get("features") or {}).items():
            feature_counter[f"{k}={v}"] += 1

    mc_status_counter = Counter(r.get("mc_status") for r in recent if r.get("mc_status"))
    rg_counter = Counter(r.get("roadguard_status") for r in recent if r.get("roadguard_status"))

    lines: list[str] = []
    lines.append(f"# MEMORY.md — {brain.upper()}")
    lines.append("")
    lines.append(f"_Authority: `{AUTHORITY_MEMORY_REASONING_ONLY}`_")
    lines.append("")
    lines.append("## Totals")
    lines.append("")
    lines.append(f"- **Memory events recorded:** {total}")
    lines.append(f"- **Shown in profile (most recent):** {len(recent)}")
    lines.append(f"- **Resolved (have outcome):** {n_resolved}")
    if pnls:
        lines.append(
            f"- **Win / Loss / Flat:** {wins} / {losses} / {flat} "
            f"(avg pnl_pct {avg_pnl:.4f})"
        )
    lines.append("")

    if symbols:
        lines.append("## Top symbols")
        lines.append("")
        for sym, n in symbols.most_common(10):
            lines.append(f"- `{sym}` × {n}")
        lines.append("")

    if directions:
        lines.append("## Direction mix")
        lines.append("")
        for d, n in directions.most_common():
            lines.append(f"- `{d}` × {n}")
        lines.append("")

    if feature_counter:
        lines.append("## Top features observed")
        lines.append("")
        for feat, n in feature_counter.most_common(10):
            lines.append(f"- `{feat}` × {n}")
        lines.append("")

    if mc_status_counter:
        lines.append("## MC gate status seen")
        lines.append("")
        for s, n in mc_status_counter.most_common():
            lines.append(f"- `{s}` × {n}")
        lines.append("")

    if rg_counter:
        lines.append("## RoadGuard status seen")
        lines.append("")
        for s, n in rg_counter.most_common():
            lines.append(f"- `{s}` × {n}")
        lines.append("")

    lines.append(f"## Recent events (up to {recent_limit})")
    lines.append("")
    if not recent:
        lines.append("_No events yet._")
    else:
        lines.append("| when | symbol | dir | conf | decision | mc | rg | pnl_pct |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in recent:
            o = r.get("outcome") or {}
            lines.append(
                "| {at} | {sym} | {dir} | {conf} | {dec} | {mc} | {rg} | {pnl} |".format(
                    at=_fmt(r.get("created_at")),
                    sym=_fmt(r.get("symbol")),
                    dir=_fmt(r.get("direction")),
                    conf=_fmt(r.get("confidence")),
                    dec=_fmt(r.get("decision")),
                    mc=_fmt(r.get("mc_status")),
                    rg=_fmt(r.get("roadguard_status")),
                    pnl=_fmt(o.get("pnl_pct")),
                )
            )

    return "\n".join(lines)
