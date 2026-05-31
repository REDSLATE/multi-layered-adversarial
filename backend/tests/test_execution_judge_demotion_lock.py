"""Doctrine lock — execution_judge demotion (2026-05-31).

Pins the following invariants so a future agent cannot silently
re-promote `execution_judge` back to a peer seat:

  1. `execution_judge` MUST NOT appear in `required_seats()` — it's
     not a real seat, it's a doctrine output.
  2. The doctrine packet emitted by both equity strategies AND the
     crypto sidecar must label the role as `setup_quality_summary`,
     not `execution_judge` — packet `seats.execution_judge.role`
     is now the demoted string.
  3. Both packet shapes must include `advisory_only: True` and
     `blocks_execution: False` on the demoted role.
  4. `execution_ready` / `execution_judge_ready` are retained as
     backward-compat aliases for the scorecard's outcome-join over
     historical intent rows; new code reads `summary_ok` instead.

Why this lock exists:
  The execution_judge role was introduced 2026-05-17 by a prior agent
  as part of the Patent J doctrine sidecar. It was never in the
  operator's original 4-seat doctrine (Strategist · Governor · Auditor
  · Executor). The UI rendered it as a peer chip, which visually
  implied execution authority. The operator confirmed (2026-05-31)
  it was not authorized and demoted it to advisory-only.
"""
from __future__ import annotations

from shared.crypto.doctrine.crypto_brain_sidecars import _build_execution_judge as _crypto_build
from shared.doctrine.strategy_doctrines import (
    _build_gap_and_go_v1,
    _build_micro_pullback_v1,
)
from shared.seat_policy import required_seats


class _Base:
    def __init__(self, quality="REJECT", score=0.25, symbol="X", labels=None, reasons=None):
        self.quality = quality
        self.score = score
        self.symbol = symbol
        self.labels = labels or []
        self.reasons = reasons or []


def test_execution_judge_is_not_a_real_seat():
    """`required_seats()` is the seat-policy contract — only the four
    real seats live there. execution_judge MUST NOT be in it."""
    seats = set(required_seats())
    assert "execution_judge" not in seats, (
        "execution_judge was re-added to required_seats — this is the "
        "exact 2026-05-17 invention the operator demoted. Revert."
    )


def test_crypto_doctrine_packet_role_is_setup_quality_summary():
    """The crypto builder MUST label the role as
    `setup_quality_summary`, advisory_only, blocks_execution=False."""
    base = _Base()
    out = _crypto_build(base=base, labels=set(), holder=None, snapshot={})
    assert out["role"] == "setup_quality_summary"
    assert out["advisory_only"] is True
    assert out["blocks_execution"] is False


def test_equity_gap_and_go_doctrine_packet_role_is_setup_quality_summary():
    packet = _build_gap_and_go_v1({"symbol": "TSLA"}, seat_holders={})
    ej = packet["seats"]["execution_judge"]
    assert ej["role"] == "setup_quality_summary"
    assert ej["advisory_only"] is True
    assert ej["blocks_execution"] is False


def test_equity_micro_pullback_doctrine_packet_role_is_setup_quality_summary():
    packet = _build_micro_pullback_v1({"symbol": "NVDA"}, seat_holders={})
    ej = packet["seats"]["execution_judge"]
    assert ej["role"] == "setup_quality_summary"
    assert ej["advisory_only"] is True
    assert ej["blocks_execution"] is False


def test_legacy_execution_ready_field_still_present_for_scorecard_join():
    """The scorecard's outcome-join keys on `execution_judge_ready`
    (hoisted) / `execution_ready` (raw). Removing them would break
    correlation analytics over historical intent rows. Keep the alias."""
    base = _Base(quality="A_QUALITY", score=0.85)
    out = _crypto_build(
        base=base,
        labels={"EXCHANGE_LIQUIDITY_OK"},
        holder="alpha",
        snapshot={"existing_intent": {"side": "BUY"}},
    )
    assert "execution_ready" in out, "scorecard backward-compat alias removed"
    assert "summary_ok" in out, "new canonical field missing"
    # Both must agree for any new row (alias keeps reading the same source).
    assert out["execution_ready"] == out["summary_ok"]
