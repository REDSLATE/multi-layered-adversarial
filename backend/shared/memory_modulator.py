"""Memory Modulator — confidence nudge from prior similar setups.

Doctrine (2026-05-24, operator-locked):

    Memory MAY nudge confidence.
    Memory MAY NOT create direction.
    Memory MAY NOT promote HOLD.
    Memory MAY NOT bypass gates.
    Memory MAY NOT bypass ladder stage.
    Memory MAY NOT bypass RoadGuard.
    Memory MAY NOT independently size orders.

    Symmetric across all 4 brains: Alpha gets benefit first because it
    has the best resolved history, but no hardcoded Alpha favoritism.
    Camaro at 40% naturally gets dampened if its prior similar memories
    are losers. Brains with no resolved history naturally get 0.0 modulator.

    Asymmetric sample minimums (punishment > reward urgency):
      * Requires 5 close matches with `outcome=1` to upweight
      * Requires only 2 close matches with `outcome=-1` to downweight
      * If neither threshold is met → neutral 0.0

    Modulator output ∈ [-0.25, +0.10]:
      * The new confidence becomes clamp(0.0, 1.0, original + modulator).
      * Sizing flows naturally through the executor's confidence multiplier.
      * No independent size knob.

Implementation notes:
    Similarity = cosine on the `features` dict (same numeric per-bar
    signal vector each brain emits at decision time — that's literally
    "the setup"). Threshold ≥ 0.85.

    Lookback: 90 days, env-configurable.

    Modulator output is stamped on the intent's audit row as the
    `memory_modulator` sidecar with full provenance (matched_winners,
    matched_losers, mean similarity, reason).
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import BRAIN_MEMORIES


logger = logging.getLogger(__name__)


# ── operator-locked spec ───────────────────────────────────────────
ENABLED = os.environ.get("MEMORY_MODULATOR_ENABLED", "true").lower() == "true"
MODE = os.environ.get("MEMORY_MODULATOR_MODE", "symmetric")
TARGET = os.environ.get("MEMORY_MODULATOR_TARGET", "confidence_only")
MAX_UP = float(os.environ.get("MEMORY_MODULATOR_MAX_UP", "0.10"))
MAX_DOWN = float(os.environ.get("MEMORY_MODULATOR_MAX_DOWN", "-0.25"))
SIMILARITY_THRESHOLD = float(
    os.environ.get("MEMORY_MODULATOR_SIMILARITY_THRESHOLD", "0.85"),
)
LOOKBACK_DAYS = int(os.environ.get("MEMORY_MODULATOR_LOOKBACK_DAYS", "90"))
MIN_MATCHES_FOR_UPWEIGHT = int(
    os.environ.get("MEMORY_MODULATOR_MIN_MATCHES_FOR_UPWEIGHT", "5"),
)
MIN_MATCHES_FOR_DOWNWEIGHT = int(
    os.environ.get("MEMORY_MODULATOR_MIN_MATCHES_FOR_DOWNWEIGHT", "2"),
)

# Directional actions the modulator runs against. HOLD, observation,
# endorse, veto, OPEN, CLOSE etc. are skipped — modulator never creates
# direction.
DIRECTIONAL_ACTIONS = {"BUY", "SHORT"}


def _cosine(a: dict, b: dict) -> float:
    """Cosine similarity between two feature dicts. Returns 0.0 when
    either side has zero magnitude or the dicts share no keys."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    num = 0.0
    mag_a = 0.0
    mag_b = 0.0
    for k in common:
        va = float(a[k])
        vb = float(b[k])
        num += va * vb
        mag_a += va * va
        mag_b += vb * vb
    if mag_a <= 0 or mag_b <= 0:
        return 0.0
    return num / (math.sqrt(mag_a) * math.sqrt(mag_b))


def _modulator_for_counts(winners: int, losers: int, mean_win_sim: float,
                          mean_loss_sim: float) -> tuple[float, str]:
    """Compute the modulator value from match counts + mean similarities.

    Loss path triggers first (asymmetric minimums per operator spec).
    """
    # ── downweight branch (priority, asymmetric) ──
    if losers >= MIN_MATCHES_FOR_DOWNWEIGHT:
        # Scale by how strong the loss match is. Linear from 0 at
        # threshold to MAX_DOWN at perfect similarity (1.0).
        # Map [SIMILARITY_THRESHOLD, 1.0] → [0, MAX_DOWN].
        denom = max(1e-6, 1.0 - SIMILARITY_THRESHOLD)
        frac = (mean_loss_sim - SIMILARITY_THRESHOLD) / denom
        frac = max(0.0, min(1.0, frac))
        value = round(MAX_DOWN * frac, 4)
        # Loss bonus — more matched losers = stronger penalty (capped at MAX_DOWN)
        loss_bonus = min(1.0, losers / 10.0)
        value = round(value * (0.7 + 0.3 * loss_bonus), 4)
        # Clamp to MAX_DOWN ceiling
        value = max(MAX_DOWN, value)
        reason = (
            f"matched {losers} prior losers (sim={mean_loss_sim:.3f}); "
            f"dampening by {value:+.3f}"
        )
        return value, reason

    # ── upweight branch ──
    if winners >= MIN_MATCHES_FOR_UPWEIGHT:
        denom = max(1e-6, 1.0 - SIMILARITY_THRESHOLD)
        frac = (mean_win_sim - SIMILARITY_THRESHOLD) / denom
        frac = max(0.0, min(1.0, frac))
        value = round(MAX_UP * frac, 4)
        # Capped at MAX_UP
        value = min(MAX_UP, value)
        reason = (
            f"matched {winners} prior winners (sim={mean_win_sim:.3f}); "
            f"upweighting by {value:+.3f}"
        )
        return value, reason

    # ── neutral ──
    return 0.0, (
        f"insufficient matches (winners={winners}, losers={losers}); "
        f"need ≥{MIN_MATCHES_FOR_UPWEIGHT} winners or "
        f"≥{MIN_MATCHES_FOR_DOWNWEIGHT} losers to nudge"
    )


def _action_matches(historical_action: str, current_action: str) -> bool:
    """Match BUY ↔ BUY and SHORT ↔ SHORT only. We intentionally do NOT
    match BUY-then-win against SHORT-now (different setups)."""
    if not historical_action or not current_action:
        return False
    return historical_action.upper() == current_action.upper()


async def compute_memory_modulator(
    brain: str,
    symbol: str,
    action: str,
    features: dict[str, float] | None,
) -> dict:
    """Compute the memory-derived confidence modulator for an intent.

    Returns:
        {
          "modulator": float ∈ [MAX_DOWN, MAX_UP],
          "matched_winners": int,
          "matched_losers": int,
          "mean_winner_similarity": float | None,
          "mean_loser_similarity": float | None,
          "reason": str,
          "config": {threshold, lookback_days, min_winners, min_losers},
          "skipped": bool,                  # true if no nudge applied
        }

    Skips with modulator=0.0 (and `skipped=True`) when:
      * Modulator is disabled by env
      * Action is not directional (BUY / SHORT)
      * Features dict is missing or empty
    """
    if not ENABLED:
        return _skipped("modulator disabled")
    if action.upper() not in DIRECTIONAL_ACTIONS:
        return _skipped(f"non-directional action {action!r}; no nudge")
    if not features:
        return _skipped("no features on intent; no nudge")

    since = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat()

    # ── Doctrine firewall (2026-05-25) ──
    # Exclude memory_ids any brain has self-labeled `quarantine`. If
    # any quarantined memory makes it into the similarity pool, the
    # firewall is decoration. Pulled once per modulator call (each
    # intent ingest), cached implicitly via the 60s cache in the
    # cross-brain memories endpoint when brains use that path.
    try:
        from routes.runtime_cross_brain_memories import (  # noqa: WPS433
            _quarantined_memory_ids,
        )
        quarantined = await _quarantined_memory_ids(symbol)
    except Exception as e:  # noqa: BLE001
        # Fail-CLOSED on the quarantine join (operator-pinned doctrine):
        # if the firewall can't be consulted, refuse to upweight on
        # potentially-poisoned data. Return neutral.
        logger.warning(
            "memory_modulator: quarantine lookup failed (%r) — "
            "skipping nudge to honor firewall fail-closed doctrine",
            e,
        )
        return _skipped("quarantine lookup unavailable; firewall fail-closed")

    # Pull historical memories: same brain, same symbol, directional
    # action (we filter `decision.raw_action` to BUY/SHORT). 90d window.
    query: dict = {
        "brain": brain,
        "symbol": symbol.upper(),
        "decided_at": {"$gte": since},
        "decision.raw_action": {"$in": list(DIRECTIONAL_ACTIONS)},
    }
    if quarantined:
        query["memory_id"] = {"$nin": list(quarantined)}
    cursor = db[BRAIN_MEMORIES].find(
        query,
        {"_id": 0, "decision": 1, "resolution": 1, "features": 1,
         "memory_id": 1, "decision_id": 1},
    ).limit(2000)

    winner_sims: list[float] = []
    loser_sims: list[float] = []

    async for row in cursor:
        # Second-pass firewall: a memory may carry a quarantined
        # decision_id even if its own memory_id is fine. The Mongo
        # query above only excluded by memory_id; this catches the
        # decision_id case so the firewall is total.
        did = row.get("decision_id")
        if did and did in quarantined:
            continue
        hist_action = (row.get("decision") or {}).get("raw_action", "")
        if not _action_matches(hist_action, action):
            continue
        hist_features = row.get("features") or {}
        if not hist_features:
            continue
        sim = _cosine(features, hist_features)
        if sim < SIMILARITY_THRESHOLD:
            continue
        outcome = (row.get("resolution") or {}).get("outcome")
        if outcome == 1:
            winner_sims.append(sim)
        elif outcome == -1:
            loser_sims.append(sim)
        # outcome == 0 (HOLD/push) is ignored — neutral, can't grade.

    mean_w = sum(winner_sims) / len(winner_sims) if winner_sims else 0.0
    mean_l = sum(loser_sims) / len(loser_sims) if loser_sims else 0.0
    value, reason = _modulator_for_counts(
        winners=len(winner_sims), losers=len(loser_sims),
        mean_win_sim=mean_w, mean_loss_sim=mean_l,
    )

    return {
        "modulator": value,
        "matched_winners": len(winner_sims),
        "matched_losers": len(loser_sims),
        "mean_winner_similarity": round(mean_w, 4) if winner_sims else None,
        "mean_loser_similarity": round(mean_l, 4) if loser_sims else None,
        "reason": reason,
        "config": {
            "similarity_threshold": SIMILARITY_THRESHOLD,
            "lookback_days": LOOKBACK_DAYS,
            "min_matches_for_upweight": MIN_MATCHES_FOR_UPWEIGHT,
            "min_matches_for_downweight": MIN_MATCHES_FOR_DOWNWEIGHT,
            "max_up": MAX_UP,
            "max_down": MAX_DOWN,
        },
        "skipped": value == 0.0,
    }


def _skipped(reason: str) -> dict:
    return {
        "modulator": 0.0,
        "matched_winners": 0,
        "matched_losers": 0,
        "mean_winner_similarity": None,
        "mean_loser_similarity": None,
        "reason": reason,
        "config": {
            "similarity_threshold": SIMILARITY_THRESHOLD,
            "lookback_days": LOOKBACK_DAYS,
            "min_matches_for_upweight": MIN_MATCHES_FOR_UPWEIGHT,
            "min_matches_for_downweight": MIN_MATCHES_FOR_DOWNWEIGHT,
            "max_up": MAX_UP,
            "max_down": MAX_DOWN,
        },
        "skipped": True,
    }


def apply_to_confidence(
    original_confidence: float, modulator_value: float,
) -> float:
    """Apply the modulator value to a confidence, clamped to [0.0, 1.0].

    Doctrine: modulator may nudge — never promote HOLD to trade,
    never push confidence outside [0.0, 1.0].
    """
    return max(0.0, min(1.0, original_confidence + modulator_value))
