"""MC intent classifier — the single source of truth for "is this
brain emission an executable candidate or advisory only?"

Doctrine pin (2026-05-18): brains speak in their own shape (BUY/SELL/HOLD,
opinions, governor authority calls, oppositions). MC owns the
classification. Sidecars never decide whether their own emission is
executable — only MC does.

This module is intentionally policy-light:
  * Direction must be BUY/SELL → directional candidate
  * Symbol + lane + confidence floor must be present
  * Anything else (HOLD, opinion, unknown direction, missing lane) →
    advisory_only=True with a typed reason
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


DIRECTIONAL = {"BUY", "SELL"}
NON_DIRECTIONAL = {"HOLD", "WAIT", "NONE", "NEUTRAL", ""}


@dataclass(frozen=True)
class IntentClassification:
    executable_candidate: bool
    advisory_only: bool
    reason: str
    normalized_direction: str
    confidence: float
    lane: str
    symbol: str
    brain: str


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_brain_intent(
    intent: Dict[str, Any],
    *,
    min_exec_conf: float = 0.30,
) -> IntentClassification:
    """Classify a raw brain emission into executable_candidate or
    advisory_only with a typed reason.

    `intent` shape is permissive — sidecars send different shapes. The
    classifier reads from the first non-empty of:
      direction: `direction` | `side` | `action`
      confidence: `raw_confidence` | `confidence` | `effective_confidence`
      symbol: `symbol` | `canonical_id`
      brain: `brain` | `source`

    Reasons emitted (audit-stable strings):
      EXECUTABLE_CANDIDATE       — passes all checks
      EXECUTABLE_CANDIDATE_TOEHOLD — toehold-doctrine intent (size
                                     already clamped at the doctrine
                                     layer); floor bypassed.
      SYMBOL_MISSING             — no symbol
      NON_DIRECTIONAL_OPINION    — HOLD/WAIT/NONE/NEUTRAL/empty
      UNKNOWN_DIRECTION:<X>      — direction is something other than BUY/SELL
      CONFIDENCE_BELOW_EXEC_FLOOR  — conf < min_exec_conf
      LANE_MISSING_OR_INVALID    — lane not in {equity, crypto}
    """
    brain = str(intent.get("brain") or intent.get("source") or "unknown").lower()
    lane = str(intent.get("lane") or "").lower()
    symbol = str(intent.get("symbol") or intent.get("canonical_id") or "").strip()

    direction = str(
        intent.get("direction")
        or intent.get("side")
        or intent.get("action")
        or "HOLD"
    ).upper().strip()

    confidence = _f(
        intent.get("raw_confidence", intent.get("confidence", intent.get("effective_confidence", 0.0)))
    )

    # 2026-02-20: toehold-doctrine intents bypass the floor.
    # When the doctrine layer tagged the intent with
    # `BASELINE_ONLY_TOEHOLD`, the governor has already clamped
    # `risk_multiplier ≤ 0.20`. The confidence floor was originally a
    # "filter low-conviction noise out of FULL-size orders" guard —
    # but a toehold-size order at 0.20 conviction is the explicit
    # design ("trade tiny, learn, don't repeat"). Skipping the floor
    # here preserves the doctrine boundary: the doctrine decides
    # whether to act, the seat sizes, and the contract no longer
    # second-guesses either.
    doctrine_labels = set()
    try:
        labels_field = (intent.get("doctrine_packet") or {}).get("base_labels") or {}
        for lbl in (labels_field.get("labels") or []):
            doctrine_labels.add(str(lbl).upper())
    except Exception:  # noqa: BLE001
        pass
    is_toehold = "BASELINE_ONLY_TOEHOLD" in doctrine_labels

    if not symbol:
        return IntentClassification(False, True, "SYMBOL_MISSING", direction, confidence, lane, symbol, brain)

    if direction in NON_DIRECTIONAL:
        return IntentClassification(False, True, "NON_DIRECTIONAL_OPINION", "HOLD", confidence, lane, symbol, brain)

    if direction not in DIRECTIONAL:
        return IntentClassification(False, True, f"UNKNOWN_DIRECTION:{direction}", direction, confidence, lane, symbol, brain)

    if confidence < min_exec_conf and not is_toehold:
        return IntentClassification(False, True, "CONFIDENCE_BELOW_EXEC_FLOOR", direction, confidence, lane, symbol, brain)

    if lane not in {"equity", "crypto"}:
        return IntentClassification(False, True, "LANE_MISSING_OR_INVALID", direction, confidence, lane, symbol, brain)

    reason = "EXECUTABLE_CANDIDATE_TOEHOLD" if is_toehold else "EXECUTABLE_CANDIDATE"
    return IntentClassification(True, False, reason, direction, confidence, lane, symbol, brain)
