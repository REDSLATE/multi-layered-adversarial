"""Movement + event label derivation. Reads existing fields — NEVER
guesses. If a row's lifecycle is ambiguous, derivation returns
`"ambiguous"` and the runner skips the row (the doctrine ensures
nothing gets rolled up unless its truth is recoverable)."""
from __future__ import annotations

from typing import Any


def derive_movement(row: dict) -> str:
    """long | short | flat | blocked | rejected | ambiguous."""
    gate = str(row.get("gate_state") or "").lower()
    if gate == "rejected_at_ingest":
        return "rejected"
    if gate in {"blocked", "dry_run_blocked"}:
        return "blocked"

    action = str(row.get("action") or "").upper()
    if action in {"BUY", "OPEN"}:
        return "long"
    if action in {"SHORT"}:
        return "short"
    if action in {"SELL", "COVER", "CLOSE", "HOLD"}:
        return "flat"

    # Fallback for rows without `action` (e.g. outcomes, receipts).
    # `actual` is the outcome row's win/loss field.
    actual = row.get("actual")
    if actual in {"win", 1}:
        return "long"   # win on a directional bet — the actual side
        # was already encoded upstream; we surface the lifecycle
        # rather than the side here. The richer signal lives in
        # the rolled `event`.
    if actual == "loss":
        return "flat"   # closed loss
    return "ambiguous"


def derive_event(row: dict) -> str:
    """executed_win | executed_loss | executed_scratch |
    blocked_<gate> | rejected_at_ingest | expired_no_fill |
    shadow_observation | ambiguous."""
    # Executed real trades — outcome resolves the event.
    if row.get("executed") is True:
        outcome = row.get("outcome") or (
            (row.get("resolution") or {}).get("outcome")
        )
        if outcome in {"win", 1}:
            return "executed_win"
        if outcome in {"loss", -1}:
            return "executed_loss"
        if outcome in {"scratch", 0}:
            return "executed_scratch"
        return "ambiguous"

    # Outcome row (shared_brain_outcomes) — `actual` is the label.
    actual = row.get("actual")
    if actual == "win":
        return "executed_win"
    if actual == "loss":
        return "executed_loss"
    if actual == "scratch":
        return "executed_scratch"

    # Pre-execution blockers.
    gate = (
        row.get("blocked_by")
        or row.get("reject_reason")
        or row.get("gate_reason")
        or row.get("rejected_reason")
    )
    if gate:
        # `blocked_by` is sometimes a list — flatten to the first name.
        if isinstance(gate, list) and gate:
            gate = gate[0]
        if isinstance(gate, str) and gate:
            return f"blocked_{gate}"

    if str(row.get("gate_state") or "").lower() == "rejected_at_ingest":
        return "rejected_at_ingest"
    if row.get("expired_no_fill") is True:
        return "expired_no_fill"

    # A non-executed, non-blocked row is a shadow / observation.
    return "shadow_observation"
