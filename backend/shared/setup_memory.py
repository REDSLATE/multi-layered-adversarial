"""Setup Memory — feedback loop that adjusts brain confidence at
gate-time based on (brain, setup) report-card history.

Doctrine (operator pin 2026-02-20):
    "Then Paradox learns: Hellcat is good at ETH breakdowns; GTO is
     weak during chop; Barracuda is useful after oversold spikes;
     Camino needs trend confirmation."

How it works:
    1. At ingest, classify the intent's setup (via lessons.classify_setup).
    2. Look up the (brain, setup) win-rate in the last N resolved
       lessons.
    3. Return a confidence multiplier:
         win_rate >= 0.60  →  1.10×  (proven setup, slight boost)
         win_rate >= 0.45  →  1.00×  (neutral)
         win_rate >= 0.30  →  0.80×  (weak setup, throttle)
         win_rate <  0.30  →  0.50×  (broken setup, deep throttle)
       Bounded to [0.50, 1.20] so memory never silently zeros a
       brain or runs away with confidence boosts.
    4. Sample size guard: < `min_sample_size` resolved lessons → 1.00×
       (no signal, don't pretend we have one).

Doctrine guard rails (pinned by tests):
    * READ-ONLY against report cards / lessons.
    * Only mutates `intent["evidence"]["setup_memory"]` and
      `intent["confidence"]`. Never touches action, gate_state,
      or any pipeline key.
    * KILL SWITCH on `runtime_flags.setup_memory_enabled` (default
      OFF). When disabled, the helper returns 1.0× without even
      reading the report card.
"""
from __future__ import annotations

import logging
from typing import Optional

from db import db
from shared.lessons.setup_classifier import classify_setup
from shared.report_cards import build_report_card


_RUNTIME_FLAGS = "runtime_flags"


_log = logging.getLogger("risedual.setup_memory")


# Bounds and bucket thresholds — operator-tunable later via runtime flags.
MULT_BOUND_MIN = 0.50
MULT_BOUND_MAX = 1.20
MIN_SAMPLE_SIZE = 5         # below this, return 1.0× (no signal)
WINDOW_INTENTS = 200        # how many recent lessons to consider


_BUCKETS = [
    # (min_win_rate, multiplier, label)
    (0.60, 1.10, "proven"),
    (0.45, 1.00, "neutral"),
    (0.30, 0.80, "weak"),
    (0.00, 0.50, "broken"),
]


def _bucket_for(win_rate: float) -> tuple[float, str]:
    for thr, mult, label in _BUCKETS:
        if win_rate >= thr:
            return mult, label
    return 0.50, "broken"


async def setup_memory_enabled() -> bool:
    """Check the kill-switch in runtime_flags. Default = False."""
    try:
        row = await db[_RUNTIME_FLAGS].find_one(
            {"key": "setup_memory_enabled"}, {"_id": 0, "value": 1},
        )
    except Exception:  # noqa: BLE001
        return False
    if not row:
        return False
    val = row.get("value")
    return val is True or str(val).lower() in ("true", "1", "on", "yes")


async def compute_adjustment(
    *,
    stack: str,
    lane: str,
    action: str,
    research_signals: Optional[list[dict]] = None,
) -> dict:
    """Return {"multiplier": float, "setup_id": str, "win_rate": float|None,
    "sample_size": int, "bucket": str, "reason": str}.

    Multiplier is bounded to [MULT_BOUND_MIN, MULT_BOUND_MAX]. Reason
    is a short token suitable for surfacing in the post-mortem chip
    ("proven_setup", "neutral_history", "insufficient_samples", etc.).
    """
    setup_id = classify_setup(action, research_signals)

    if setup_id == "abstain":
        # HOLD intents don't flow through the bridges in production
        # (the bridges reject HOLD); guard anyway.
        return {
            "multiplier": 1.0,
            "setup_id": setup_id,
            "win_rate": None,
            "sample_size": 0,
            "bucket": "neutral",
            "reason": "abstain_no_adjustment",
        }

    card = await build_report_card(
        stack=stack, lane=lane, setup_id=setup_id, limit=WINDOW_INTENTS,
    )
    overall = card.get("overall") or {}
    sample = int(overall.get("intents_resolved") or 0)
    win_rate = overall.get("win_rate")

    if sample < MIN_SAMPLE_SIZE or win_rate is None:
        return {
            "multiplier": 1.0,
            "setup_id": setup_id,
            "win_rate": win_rate,
            "sample_size": sample,
            "bucket": "neutral",
            "reason": "insufficient_samples",
        }

    mult, bucket = _bucket_for(float(win_rate))
    mult = max(MULT_BOUND_MIN, min(MULT_BOUND_MAX, mult))
    return {
        "multiplier": round(mult, 3),
        "setup_id": setup_id,
        "win_rate": win_rate,
        "sample_size": sample,
        "bucket": bucket,
        "reason": f"{bucket}_setup",
    }


async def apply_setup_memory(intent: dict) -> None:
    """Mutate `intent` in place: apply the setup-memory adjustment
    and stamp the audit trail. Best-effort — any failure is logged
    and the intent emits unchanged.

    Pipeline:
      1. Kill switch check. If OFF → no-op (only stamps the audit
         marker so the post-mortem can show "memory disabled").
      2. Compute the adjustment for (brain, lane, action, research).
      3. Multiply `intent["confidence"]` by the multiplier, clamped
         to [0.0, 1.0].
      4. Stamp `intent["evidence"]["setup_memory"]` with the full
         lookup result + the pre/post confidence values for audit.
    """
    ev = intent.setdefault("evidence", {})

    enabled = await setup_memory_enabled()
    if not enabled:
        ev["setup_memory"] = {
            "applied": False,
            "reason": "kill_switch_off",
            "multiplier": 1.0,
        }
        return

    try:
        block = await compute_adjustment(
            stack=intent.get("stack") or "?",
            lane=intent.get("lane") or "?",
            action=intent.get("action") or "",
            research_signals=ev.get("research_signals") or [],
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("setup_memory compute failed: %s", e)
        ev["setup_memory"] = {
            "applied": False,
            "reason": "compute_error",
            "error": str(e)[:200],
            "multiplier": 1.0,
        }
        return

    mult = float(block.get("multiplier") or 1.0)
    pre = float(intent.get("confidence") or 0.0)
    post = max(0.0, min(1.0, pre * mult))
    intent["confidence"] = round(post, 4)

    ev["setup_memory"] = {
        "applied": True,
        **block,
        "confidence_pre": round(pre, 4),
        "confidence_post": round(post, 4),
    }
