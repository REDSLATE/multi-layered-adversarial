"""Characterization tests for `shared.council._governance_verdict`.

The verdict matrix is the heart of the council's graduated authority
system — this test suite pins every verdict code so changes to council
logic cannot silently drift the semantics.

Doctrine (2026-02-20 operator patch):

    Brain      = opinion only
    Seat       = restriction authority
    Governor   = modifier
    RoadGuard  = hard stop

Per the operator's doctrine, the governor seat is a MODIFIER, not a
final-block authority. Every governor-derived reason — including
GOVERNOR_HARD_VETO, GOVERNOR_SEAT_VACANT, and SOFT_DISSENT_BELOW_FLOOR
— is downgraded to RISK_DOWN_ONLY (`allowed=True` with a per-reason
sizing penalty). Only RoadGuard / structural reasons (KILL_SWITCH,
BROKER_UNAVAILABLE, AUTH_MISSING, SYMBOL_UNRESOLVED, MAX_EXPOSURE,
PDT_BLOCK, DUPLICATE_POSITION) may hard-block at the council layer.

  * GOVERNOR_SEAT_VACANT      → RISK_DOWN_ONLY (modifier, 0.50×)
  * GOVERNOR_OFFLINE          → RISK_DOWN_ONLY (modifier, 0.50×)
  * NO_STANCE_LOW_EFFECTIVE_CONF → RISK_DOWN_ONLY (modifier, 0.50×)
  * GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT → pass, size = no_stance_size_mult
  * GOVERNOR_HARD_VETO        → RISK_DOWN_ONLY (modifier, 0.20×)
  * SOFT_DISSENT_BELOW_FLOOR  → RISK_DOWN_ONLY (modifier, 0.20×)
  * SOFT_DISSENT_DOWNWEIGHTED → pass, size = dissent_size_mult (clamped)
  * NO_GOVERNOR_DISSENT       → pass, size = momentum_weighting (clamped)
"""
from __future__ import annotations

import pytest

from shared.council import (
    COUNCIL_POLICY,
    GOVERNOR_HARD_VETO_RISK_MULTIPLIER,
    GOVERNOR_SILENCE_RISK_MULTIPLIER,
    GOVERNOR_VACANT_RISK_MULTIPLIER,
    RESTRICTION_SOURCE_GOVERNOR,
    _governance_verdict,
    governor_blocks_execution,
    governor_risk_multiplier,
)


# Tripwire suite: locked council-verdict semantics. See pytest.ini.
pytestmark = pytest.mark.tripwire


EQUITY = COUNCIL_POLICY["equity"]
CRYPTO = COUNCIL_POLICY["crypto"]


def _intent(conf: float = 0.7) -> dict:
    return {"intent_id": "i1", "symbol": "AAPL", "action": "BUY", "confidence": conf}


# ─────────────── FATAL vs SILENCE taxonomy ─────────────────


def test_governor_blocks_execution_roadguards_only():
    """Only RoadGuard / structural reasons block. ALL governor-derived
    reasons (including HARD_VETO and SEAT_VACANT) are non-fatal per
    2026-02-20 doctrine — governor is a modifier, not a hard-stop."""
    # RoadGuards: must still block.
    for r in ("KILL_SWITCH_ACTIVE", "BROKER_UNAVAILABLE", "AUTH_MISSING",
              "SYMBOL_UNRESOLVED", "MAX_EXPOSURE_EXCEEDED",
              "PDT_BLOCK", "DUPLICATE_POSITION"):
        assert governor_blocks_execution(r) is True, f"{r} must be FATAL (RoadGuard)"
    # ALL governor-derived reasons: must NOT block (now modifiers).
    for r in ("GOVERNOR_HARD_VETO", "GOVERNOR_SEAT_VACANT",
              "GOVERNOR_OFFLINE", "NO_STANCE_LOW_EFFECTIVE_CONF",
              "GOVERNOR_NO_STANCE", "SOFT_DISSENT_BELOW_FLOOR",
              "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT",
              "NO_GOVERNOR_DISSENT", None, "", "  "):
        assert governor_blocks_execution(r) is False, (
            f"{r} must NOT be FATAL — governor is a modifier per "
            f"2026-02-20 doctrine"
        )


def test_governor_blocks_execution_case_insensitive():
    assert governor_blocks_execution("kill_switch_active") is True
    assert governor_blocks_execution("  GOVERNOR_HARD_VETO  ") is False


def test_governor_risk_multiplier_per_reason_penalties():
    """Per-reason sizing penalty. Hard-veto and below-floor are the
    strongest downsize (0.20×); silence/vacant are milder (0.50×)."""
    # 0.20× — historically hard-blocks, now strongest downsize
    for r in ("GOVERNOR_HARD_VETO", "SOFT_DISSENT_BELOW_FLOOR"):
        assert governor_risk_multiplier(r) == GOVERNOR_HARD_VETO_RISK_MULTIPLIER
    # 0.50× — silence / no-stance / vacant seat
    for r in ("GOVERNOR_OFFLINE", "NO_STANCE_LOW_EFFECTIVE_CONF",
              "GOVERNOR_NO_STANCE", "GOVERNOR_SEAT_VACANT"):
        expected = (
            GOVERNOR_VACANT_RISK_MULTIPLIER if r == "GOVERNOR_SEAT_VACANT"
            else GOVERNOR_SILENCE_RISK_MULTIPLIER
        )
        assert governor_risk_multiplier(r) == expected


def test_governor_risk_multiplier_default_one():
    """Unknown / passing reasons return 1.00 (no size penalty)."""
    for r in ("NO_GOVERNOR_DISSENT", "SOFT_DISSENT_DOWNWEIGHTED", None, ""):
        assert governor_risk_multiplier(r) == 1.00


# ─────────────── verdict matrix ─────────────────


def test_governor_seat_vacant_now_risk_down():
    """2026-02-20 doctrine: a vacant governor seat means "no modifier
    appointed", which per doctrine = no modification, not freeze."""
    v = _governance_verdict(_intent(), gov_norm=None, governor_alive=True,
                            governor_holder=None, policy=EQUITY)
    assert v["allowed"] is True, "vacant seat must not block (governor is modifier-only)"
    assert v["reason"] == "GOVERNOR_SEAT_VACANT"
    assert v["execution_effect"] == "RISK_DOWN_ONLY"
    assert v["display_status"] == "RISK_DOWN"
    assert v["risk_multiplier"] > 0.0
    assert v["restriction_source"] == RESTRICTION_SOURCE_GOVERNOR


def test_governor_offline_risk_down_not_block():
    """Governor offline is SILENCE → downsize, not block."""
    v = _governance_verdict(_intent(), gov_norm=None, governor_alive=False,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "GOVERNOR_OFFLINE"
    assert v["execution_effect"] == "RISK_DOWN_ONLY"
    assert v["display_status"] == "RISK_DOWN"
    assert v["risk_multiplier"] > 0.0
    assert v["restriction_source"] == RESTRICTION_SOURCE_GOVERNOR
    # Silence multiplier 0.5 clamps to lane MAX_DOWNWEIGHT (0.6) → 0.6.
    assert v["risk_multiplier"] == pytest.approx(
        max(EQUITY["MAX_DOWNWEIGHT"], GOVERNOR_SILENCE_RISK_MULTIPLIER)
    )
    assert v["record_pushback"] is True


def test_governor_alive_no_stance_high_conf_soft_downweights():
    v = _governance_verdict(_intent(conf=0.9), gov_norm=None, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT"
    assert v["execution_effect"] == "ALLOW"
    assert v["disagreement"] is True
    assert v["risk_multiplier"] > 0.0
    expected_eff = 0.9 * EQUITY["GOVERNOR_NO_STANCE_CONF_MULT"]
    assert v["effective_conf"] == pytest.approx(expected_eff)


def test_no_stance_low_conf_risk_down_not_block():
    """NO_STANCE_LOW_EFFECTIVE_CONF is SILENCE → downsize."""
    very_low = EQUITY["MIN_EXECUTOR_CONF_FLOOR"] / max(EQUITY["GOVERNOR_NO_STANCE_CONF_MULT"], 0.01) - 0.01
    very_low = max(0.0, very_low)
    v = _governance_verdict(_intent(conf=very_low), gov_norm=None, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "NO_STANCE_LOW_EFFECTIVE_CONF"
    assert v["execution_effect"] == "RISK_DOWN_ONLY"
    assert v["display_status"] == "RISK_DOWN"
    assert v["risk_multiplier"] > 0.0


def test_hard_veto_now_risk_down_not_block():
    """2026-02-20: GOVERNOR_HARD_VETO is the strongest modifier (0.20×
    downsize) but NO LONGER blocks. Per doctrine, the brain holding
    the governor seat cannot final-veto an intent — only seat-policy
    and RoadGuards can. The hard-veto signal still records pushback
    and reduces size sharply, so the operator sees the dissent on the
    ledger."""
    gov_norm = {"veto": True, "executable": False, "confidence": 0.99, "stance": "VETO"}
    v = _governance_verdict(_intent(), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True, "hard veto must not block (governor is modifier-only)"
    assert v["reason"] == "GOVERNOR_HARD_VETO"
    assert v["execution_effect"] == "RISK_DOWN_ONLY"
    assert v["display_status"] == "RISK_DOWN"
    assert v["disagreement"] is True
    assert v["risk_multiplier"] > 0.0
    # Pinned at 0.20× — the most aggressive size penalty.
    assert v["risk_multiplier"] == pytest.approx(
        max(EQUITY["MAX_DOWNWEIGHT"], GOVERNOR_HARD_VETO_RISK_MULTIPLIER)
    )
    assert v["restriction_source"] == RESTRICTION_SOURCE_GOVERNOR


def test_veto_below_hard_threshold_treated_as_soft_dissent():
    # Veto bit set but conf is below GOVERNOR_HARD_VETO_THRESHOLD →
    # downgrades to soft dissent (with executor conf above floor it
    # downweights rather than blocks).
    low_veto_conf = EQUITY["GOVERNOR_HARD_VETO_THRESHOLD"] - 0.1
    gov_norm = {"veto": True, "executable": False, "confidence": low_veto_conf,
                "stance": "DISSENT"}
    v = _governance_verdict(_intent(conf=0.9), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "SOFT_DISSENT_DOWNWEIGHTED"
    assert v["execution_effect"] == "ALLOW"
    assert v["disagreement"] is True
    assert v["risk_multiplier"] > 0.0


def test_soft_dissent_below_floor_risk_down_not_block():
    """SOFT_DISSENT_BELOW_FLOOR is non-fatal silence."""
    gov_norm = {"veto": False, "executable": False, "confidence": 0.5, "stance": "DISSENT"}
    floor = EQUITY["MIN_EXECUTOR_CONF_FLOOR"]
    dissent_cm = EQUITY["GOVERNOR_DISSENT_CONF_MULT"]
    weak_conf = (floor / max(dissent_cm, 0.01)) - 0.05
    v = _governance_verdict(_intent(conf=max(0.0, weak_conf)), gov_norm=gov_norm,
                            governor_alive=True, governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "SOFT_DISSENT_BELOW_FLOOR"
    assert v["execution_effect"] == "RISK_DOWN_ONLY"
    assert v["display_status"] == "RISK_DOWN"
    assert v["risk_multiplier"] > 0.0


def test_no_governor_dissent_passes_with_momentum_weighting():
    gov_norm = {"veto": False, "executable": True, "confidence": 0.8, "stance": "ENDORSE"}
    v = _governance_verdict(_intent(conf=0.7), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "NO_GOVERNOR_DISSENT"
    assert v["execution_effect"] == "ALLOW"
    assert v["disagreement"] is False
    expected = min(EQUITY["MAX_UPWEIGHT"], max(EQUITY["MAX_DOWNWEIGHT"], EQUITY["MOMENTUM_WEIGHTING"]))
    assert v["risk_multiplier"] == pytest.approx(expected)


def test_crypto_policy_uses_crypto_thresholds():
    gov_norm = {"veto": False, "executable": True, "confidence": 0.8, "stance": "ENDORSE"}
    v = _governance_verdict(_intent(conf=0.7), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=CRYPTO)
    assert v["allowed"] is True
    assert v["reason"] == "NO_GOVERNOR_DISSENT"
    expected = min(CRYPTO["MAX_UPWEIGHT"], max(CRYPTO["MAX_DOWNWEIGHT"], CRYPTO["MOMENTUM_WEIGHTING"]))
    assert v["risk_multiplier"] == pytest.approx(expected)


def test_stance_strings_treated_as_disagreement():
    for stance in ("VETO", "DISSENT", "RISK_DOWN", "HOLD", "REJECT", "ABSTAIN"):
        gov_norm = {"veto": False, "executable": None, "confidence": 0.6, "stance": stance}
        v = _governance_verdict(_intent(conf=0.9), gov_norm=gov_norm, governor_alive=True,
                                governor_holder="chevelle", policy=EQUITY)
        assert v["disagreement"] is True, f"stance {stance!r} should signal disagreement"
        # High conf keeps it allowed (DOWNWEIGHTED); below-floor cases
        # land in the new RISK_DOWN_ONLY bucket.
        assert v["reason"] in {"SOFT_DISSENT_DOWNWEIGHTED", "SOFT_DISSENT_BELOW_FLOOR"}
        assert v["allowed"] is True

