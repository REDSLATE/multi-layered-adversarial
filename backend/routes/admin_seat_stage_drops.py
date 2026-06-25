"""Top seat-stage drops — operator diagnostic.

Operator pin (2026-02-23): Funnel shows 94% of emitted intents
dropping between Emitted and Seat Approved. The structural baseline
is ~75% (4 brains × 1 executor seat = 3-in-4 drop by design via
`brain_not_current_seat_holder`). The remaining ~19% is the
operator-actionable bucket: doctrine too tight, confidence floor
too tight, runtime seat issues. This endpoint splits the 94% by
canonical reason + brain + lane + seat so the operator can answer
"structural vs real rejection?" in one glance.

Returns the last `hours` of `pipeline_receipts` filtered to
`restriction_source == "seat"` (the seat layer is the only one
that produces this stage's rejections in the unified pipeline).

Canonical reason buckets (collapses dynamic suffixes like
`brain_not_current_seat_holder:gto!=camino@PASCHAR` → `brain_not_
current_seat_holder`):

  * brain_not_current_seat_holder   → EXPECTED_ADVISOR_DROP
      (structural — 1 executor + 3 advisors; ~75% baseline)
  * below_seat_confidence_min       → THRESHOLD_TOO_TIGHT
      (confidence floor; tunable via per-seat conf_min)
  * brain_not_trusted_for_seat      → RUNTIME_SEAT_ISSUE
      (trust map; check `PARADOX_V2_SEAT_TRUSTED` collection)
  * executor_seat_vacant            → RUNTIME_SEAT_ISSUE
      (no brain holds the seat — operator should assign)
  * paradox_v3_waiting_for_trigger  → V3_WAIT_PARKED
      (intentional; intent re-fires on trigger)
  * unknown_lane                    → RUNTIME_SEAT_ISSUE
      (bad lane name in emit — bug)
  * (everything else)               → OTHER

Authentication: admin JWT.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


router = APIRouter(prefix="/admin/execution-funnel", tags=["execution-funnel"])


# ── Canonical reason + interpretation map ─────────────────────────
# When we add a new seat-reason in seat_policy.py, EXTEND this map
# at the same commit. Drift here means the tile silently buckets
# new reasons under OTHER — operator sees a growing "other" wedge
# without knowing what it is.
CANONICAL_REASONS: dict[str, dict[str, str]] = {
    "brain_not_current_seat_holder": {
        "category":       "EXPECTED_ADVISOR_DROP",
        "interpretation": "Non-executor brain emitting on a seat it doesn't own. Structural by design — 1 executor + 3 advisors means ~75% of raw emits land here.",
        "action":         "leave_alone_if_dominant",
    },
    "below_seat_confidence_min": {
        "category":       "THRESHOLD_TOO_TIGHT",
        "interpretation": "Effective confidence (post-consensus-boost) under the seat's conf_min floor. Tunable via per-seat conf_min.",
        "action":         "consider_lowering_conf_min",
    },
    "brain_not_trusted_for_seat": {
        "category":       "RUNTIME_SEAT_ISSUE",
        "interpretation": "Brain isn't in the seat's trust map. Check `paradox_v2_seat_trusted` collection.",
        "action":         "verify_trust_map",
    },
    "executor_seat_vacant": {
        "category":       "RUNTIME_SEAT_ISSUE",
        "interpretation": "No brain currently holds this executor seat. Assign one from the Quick Seat Switches panel.",
        "action":         "assign_seat_holder",
    },
    "paradox_v3_wait_for_trigger": {
        "category":       "V3_WAIT_PARKED",
        "interpretation": "v3 WAIT_FOR_TRIGGER plan intentionally parked. Re-fires when the trigger price is crossed.",
        "action":         "informational_only",
    },
    "paradox_v3_wait_confirmation": {
        "category":       "V3_WAIT_PARKED",
        "interpretation": "v3 WAIT_CONFIRMATION plan intentionally parked. Re-fires when the confirmation candle prints.",
        "action":         "informational_only",
    },
    "unknown_lane": {
        "category":       "RUNTIME_SEAT_ISSUE",
        "interpretation": "Emit referenced a lane that doesn't exist. Likely a brain-runtime bug.",
        "action":         "file_bug",
    },
}

CATEGORY_COLOR: dict[str, str] = {
    "EXPECTED_ADVISOR_DROP": "neutral",
    "THRESHOLD_TOO_TIGHT":   "warn",
    "RUNTIME_SEAT_ISSUE":    "error",
    "V3_WAIT_PARKED":        "info",
    "OTHER":                 "neutral",
}


def _canonicalize_reason(raw: str) -> str:
    """Strip dynamic suffixes from seat-stage reasons.

    `brain_not_current_seat_holder:gto!=camino@PASCHAR`
        → `brain_not_current_seat_holder`
    `below_seat_confidence_min:0.412<0.700 (base ...)`
        → `below_seat_confidence_min`
    `brain_not_trusted_for_seat:gto->PASCHAR`
        → `brain_not_trusted_for_seat`
    """
    if not raw:
        return ""
    # Reasons are formatted as `<canonical>:<dynamic>` or just
    # `<canonical>` — split on the first colon.
    head = raw.split(":", 1)[0]
    # Defensive: strip surrounding whitespace + trailing punctuation
    # that occasionally sneaks into receipt reasons.
    return head.strip().rstrip(".:- ")


def _extract_seat_from_reason(raw: str) -> Optional[str]:
    """Pull the seat id from canonical reason suffixes when present.

    `brain_not_current_seat_holder:gto!=camino@PASCHAR` → "PASCHAR"
    `executor_seat_vacant:PASCHAR`                      → "PASCHAR"
    `brain_not_trusted_for_seat:gto->PASCHAR`           → "PASCHAR"

    Returns None when the reason doesn't carry a seat id.
    """
    if not raw or ":" not in raw:
        return None
    payload = raw.split(":", 1)[1]
    if "@" in payload:
        return payload.split("@", 1)[1].split()[0].strip()
    if "->" in payload:
        return payload.split("->", 1)[1].split()[0].strip()
    # `executor_seat_vacant:PASCHAR` — payload IS the seat id.
    if not any(c in payload for c in "<>=!"):
        return payload.split()[0].strip()
    return None


@router.get("/seat-stage-drops")
async def seat_stage_drops(
    hours: int = Query(default=24, ge=1, le=168),
    lane:  Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Top seat-stage rejection reasons (canonical + by brain + by lane + by seat).

    Response shape:
        {
          "window_hours": 24,
          "now": "<ISO>",
          "total_seat_rejected": <int>,
          "total_emitted":       <int>,   # for the % denominator
          "structural_pct":      <float>, # brain_not_current_seat_holder share
          "actionable_pct":      <float>, # 1 - structural - v3_wait
          "reasons": [
            {
              "reason": "brain_not_current_seat_holder",
              "count":  4612,
              "pct":    0.777,
              "category": "EXPECTED_ADVISOR_DROP",
              "color":  "neutral",
              "interpretation": "...",
              "action": "leave_alone_if_dominant"
            },
            ...
          ],
          "by_brain": [{"brain": "camino", "rejected": 1245, "top_reason": "brain_not_current_seat_holder"}, ...],
          "by_lane":  [{"lane": "equity", "rejected": 3210}, ...],
          "by_seat":  [{"seat": "PASCHAR", "lane": "equity", "rejected": 1820}, ...]
        }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q: dict = {"ts": {"$gte": cutoff}, "restriction_source": "seat"}
    if lane:
        q["lane"] = lane.lower()

    # Pull only the fields we need. The 50k cap matches
    # pipeline_blocker_histogram — the operator's pipeline rarely
    # exceeds this per 24h; if it does, the window can be narrowed.
    rows = await db["pipeline_receipts"].find(
        q,
        {
            "_id": 0,
            "brain_id": 1,
            "lane":     1,
            "symbol":   1,
            "final_reason": 1,
            "reason":   1,
            "ts":       1,
        },
    ).sort("ts", -1).to_list(50000)

    # Total emitted = ALL receipts in the window (including those
    # that got past the seat). Needed so the operator can compute
    # "94% drop" against the same denominator the funnel uses.
    total_emitted = await db["pipeline_receipts"].count_documents(
        {"ts": {"$gte": cutoff}} | ({"lane": lane.lower()} if lane else {})
    )

    reason_counter: Counter = Counter()
    by_brain: Counter = Counter()
    by_lane:  Counter = Counter()
    by_seat:  Counter = Counter()
    # Per-brain top-reason tracker — show the operator what's
    # dominantly killing each brain's emits.
    brain_reason: dict[str, Counter] = defaultdict(Counter)
    # Per-seat lane attribution so the tile can render `PASCHAR
    # (equity)` instead of an ambiguous bare seat id.
    seat_lane: dict[str, str] = {}

    for r in rows:
        raw_reason = r.get("final_reason") or r.get("reason") or ""
        canonical = _canonicalize_reason(raw_reason)
        reason_counter[canonical] += 1
        b = (r.get("brain_id") or "?").lower()
        ln = (r.get("lane") or "?").lower()
        by_brain[b] += 1
        by_lane[ln] += 1
        brain_reason[b][canonical] += 1
        seat = _extract_seat_from_reason(raw_reason)
        if seat:
            by_seat[seat] += 1
            seat_lane.setdefault(seat, ln)

    total_rejected = sum(reason_counter.values())

    # Build the reasons table with interpretation baked in.
    reasons_out = []
    for reason, count in reason_counter.most_common():
        meta = CANONICAL_REASONS.get(reason, {
            "category":       "OTHER",
            "interpretation": "Unmapped reason — extend CANONICAL_REASONS in `routes/admin_seat_stage_drops.py` when new seat-stage codes are added.",
            "action":         "review_in_code",
        })
        category = meta["category"]
        reasons_out.append({
            "reason":         reason,
            "count":          count,
            "pct":            (count / total_rejected) if total_rejected else 0.0,
            "category":       category,
            "color":          CATEGORY_COLOR.get(category, "neutral"),
            "interpretation": meta["interpretation"],
            "action":         meta["action"],
        })

    structural = reason_counter.get("brain_not_current_seat_holder", 0)
    v3_wait    = reason_counter.get("paradox_v3_waiting_for_trigger", 0)
    if total_rejected:
        structural_pct = structural / total_rejected
        v3_wait_pct    = v3_wait    / total_rejected
        actionable_pct = max(0.0, 1.0 - structural_pct - v3_wait_pct)
    else:
        # Empty window — emit zeros across the board. Without this
        # guard `1.0 - 0 - 0 = 1.0` claims "100% actionable" even
        # when there's nothing to act on.
        structural_pct = 0.0
        v3_wait_pct    = 0.0
        actionable_pct = 0.0

    by_brain_out = []
    for b, n in by_brain.most_common():
        top = brain_reason[b].most_common(1)
        by_brain_out.append({
            "brain":      b,
            "rejected":   n,
            "top_reason": (top[0][0] if top else None),
        })

    by_seat_out = [
        {"seat": s, "lane": seat_lane.get(s, "?"), "rejected": n}
        for s, n in by_seat.most_common()
    ]

    return {
        "window_hours":        hours,
        "now":                 datetime.now(timezone.utc).isoformat(),
        "total_seat_rejected": total_rejected,
        "total_emitted":       total_emitted,
        "structural_pct":      round(structural_pct, 4),
        "v3_wait_pct":         round(v3_wait_pct, 4),
        "actionable_pct":      round(actionable_pct, 4),
        "reasons":             reasons_out,
        "by_brain":            by_brain_out,
        "by_lane":             [{"lane": ln, "rejected": n}
                                for ln, n in by_lane.most_common()],
        "by_seat":             by_seat_out,
        "doctrine_note": (
            "Seat-stage filters intents AFTER doctrine + brain layers. "
            "brain_not_current_seat_holder is STRUCTURAL — 1 executor + 3 "
            "advisors means ~75% of raw emits land here by design. The "
            "actionable_pct % is the portion you can actually move via "
            "doctrine / conf_min tuning."
        ),
    }
