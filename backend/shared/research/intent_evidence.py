"""Shared helper for stamping Research Layer evidence onto an intent.

Used by every brain's intent bridge (redeye/GTO, chevelle/Hellcat,
and any future brain we wire). Centralized here so the doctrine guard
("research is evidence, never authority") lives in exactly one place
and so the error-containment behavior stays consistent across brains.

Doctrine pinned:
    * Writes ONLY into `intent["evidence"]["research_signals"]` +
      the `research_status` / `research_source` / `research_bars_used`
      sibling fields.
    * Never touches `action`, `confidence`, `executed`, `gate_state`,
      `dry_run_state`, or any pipeline key — see the test
      `tests/test_research_layer_2026_02_20.py::test_attach_research_does_not_touch_pipeline_keys`.
    * Best-effort — any failure (no bars, mongo down, strategy bug)
      is contained: `research_status` is set to `"error"` /
      `"no_bars_on_file"` and the intent still flows.
"""
from __future__ import annotations

import logging

from .bar_source import DEFAULT_TF_BY_LANE, load_recent_bars
from .features import build_features
from .paradox_bridge import attach_research_to_intent
from .strategy_lab import score_strategies


_log = logging.getLogger("risedual.research.intent_evidence")


async def attach_research_evidence(
    intent: dict, tf: str | None = None, limit: int = 120,
) -> None:
    """Stamp Research Layer evidence onto the given intent dict.

    Lane-aware: when `tf` is None, picks the default for the intent's
    lane from `DEFAULT_TF_BY_LANE` (crypto→1h, equity→1d). Operator
    can override per-call if a different cadence is needed.

    Mutates `intent` in place (best-effort) and returns None. Callers
    should treat this as side-effecting and idempotent — running it
    twice replaces the signals rather than appending.
    """
    symbol = intent.get("symbol")
    lane = intent.get("lane")
    if not symbol or not lane:
        return
    effective_tf = tf or DEFAULT_TF_BY_LANE.get(lane, "1h")

    try:
        bars, src = await load_recent_bars(symbol, tf=effective_tf, limit=limit)
        if not bars:
            ev = intent.setdefault("evidence", {})
            ev["research_signals"] = []
            ev["research_status"] = "no_bars_on_file"
            return

        # Reuse spread_bps if the brain (or MC's spread enrichment) put
        # one on the intent. Falls back to None — the strategy will
        # simply skip the wide-spread penalty.
        spread_bps = (
            intent.get("evidence", {}).get("source_doc", {}).get("spread_bps")
            or intent.get("spread_bps")
        )
        try:
            spread_bps = float(spread_bps) if spread_bps is not None else None
        except (TypeError, ValueError):
            spread_bps = None

        features = build_features(symbol, lane, bars, spread_bps=spread_bps)
        signals = score_strategies(features)
        attach_research_to_intent(intent, signals)
        ev = intent["evidence"]
        ev["research_status"] = "ok"
        ev["research_source"] = src
        ev["research_bars_used"] = len(bars)
        ev["research_tf"] = effective_tf
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "research evidence attach failed symbol=%s lane=%s err=%s",
            symbol, lane, e,
        )
        ev = intent.setdefault("evidence", {})
        ev["research_signals"] = []
        ev["research_status"] = "error"
        ev["research_error"] = str(e)[:200]
