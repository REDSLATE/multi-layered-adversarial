"""Characterization tests for `shared.council._governance_verdict`.

The verdict matrix is the heart of the council's graduated authority
system — this test suite pins every verdict code so the upcoming
`_evaluate_council` refactor cannot silently drift the semantics.

Doctrine:
  * GOVERNOR_SEAT_VACANT      → block, size 0
  * GOVERNOR_OFFLINE          → block, size 0
  * NO_STANCE_LOW_EFFECTIVE_CONF → block, size 0
  * GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT → pass, size = no_stance_size_mult
  * GOVERNOR_HARD_VETO        → block, size 0
  * SOFT_DISSENT_BELOW_FLOOR  → block, size 0
  * SOFT_DISSENT_DOWNWEIGHTED → pass, size = dissent_size_mult (clamped)
  * NO_GOVERNOR_DISSENT       → pass, size = momentum_weighting (clamped)
"""
from __future__ import annotations

import pytest

from shared.council import _governance_verdict, COUNCIL_POLICY


EQUITY = COUNCIL_POLICY["equity"]
CRYPTO = COUNCIL_POLICY["crypto"]


def _intent(conf: float = 0.7) -> dict:
    return {"intent_id": "i1", "symbol": "AAPL", "action": "BUY", "confidence": conf}


def test_governor_seat_vacant_blocks():
    v = _governance_verdict(_intent(), gov_norm=None, governor_alive=True,
                            governor_holder=None, policy=EQUITY)
    assert v["allowed"] is False
    assert v["reason"] == "GOVERNOR_SEAT_VACANT"
    assert v["risk_multiplier"] == 0.0
    assert v["effective_conf"] == 0.0


def test_governor_offline_blocks():
    v = _governance_verdict(_intent(), gov_norm=None, governor_alive=False,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is False
    assert v["reason"] == "GOVERNOR_OFFLINE"
    assert v["risk_multiplier"] == 0.0


def test_governor_alive_no_stance_high_conf_soft_downweights():
    v = _governance_verdict(_intent(conf=0.9), gov_norm=None, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT"
    assert v["disagreement"] is True
    assert v["risk_multiplier"] > 0.0
    # Effective conf is conf × no-stance conf-mult.
    expected_eff = 0.9 * EQUITY["GOVERNOR_NO_STANCE_CONF_MULT"]
    assert v["effective_conf"] == pytest.approx(expected_eff)


def test_governor_alive_no_stance_low_conf_blocks_on_floor():
    # Conf so low that even before suppression we're at/under floor.
    very_low = EQUITY["MIN_EXECUTOR_CONF_FLOOR"] / max(EQUITY["GOVERNOR_NO_STANCE_CONF_MULT"], 0.01) - 0.01
    very_low = max(0.0, very_low)
    v = _governance_verdict(_intent(conf=very_low), gov_norm=None, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is False
    assert v["reason"] == "NO_STANCE_LOW_EFFECTIVE_CONF"
    assert v["risk_multiplier"] == 0.0


def test_hard_veto_blocks_on_high_governor_conviction():
    gov_norm = {"veto": True, "executable": False, "confidence": 0.99, "stance": "VETO"}
    v = _governance_verdict(_intent(), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is False
    assert v["reason"] == "GOVERNOR_HARD_VETO"
    assert v["disagreement"] is True
    assert v["risk_multiplier"] == 0.0


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
    assert v["disagreement"] is True
    assert v["risk_multiplier"] > 0.0


def test_soft_dissent_below_floor_blocks():
    gov_norm = {"veto": False, "executable": False, "confidence": 0.5, "stance": "DISSENT"}
    floor = EQUITY["MIN_EXECUTOR_CONF_FLOOR"]
    dissent_cm = EQUITY["GOVERNOR_DISSENT_CONF_MULT"]
    # Pick conf so suppressed conf strictly under the floor.
    weak_conf = (floor / max(dissent_cm, 0.01)) - 0.05
    v = _governance_verdict(_intent(conf=max(0.0, weak_conf)), gov_norm=gov_norm,
                            governor_alive=True, governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is False
    assert v["reason"] == "SOFT_DISSENT_BELOW_FLOOR"
    assert v["risk_multiplier"] == 0.0


def test_no_governor_dissent_passes_with_momentum_weighting():
    gov_norm = {"veto": False, "executable": True, "confidence": 0.8, "stance": "ENDORSE"}
    v = _governance_verdict(_intent(conf=0.7), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=EQUITY)
    assert v["allowed"] is True
    assert v["reason"] == "NO_GOVERNOR_DISSENT"
    assert v["disagreement"] is False
    # Size = 1.0 × momentum_weighting, clamped to lane bounds.
    expected = min(EQUITY["MAX_UPWEIGHT"], max(EQUITY["MAX_DOWNWEIGHT"], EQUITY["MOMENTUM_WEIGHTING"]))
    assert v["risk_multiplier"] == pytest.approx(expected)


def test_crypto_policy_uses_crypto_thresholds():
    # No-dissent path with crypto policy should use crypto's momentum
    # weighting, which differs from equity by design.
    gov_norm = {"veto": False, "executable": True, "confidence": 0.8, "stance": "ENDORSE"}
    v = _governance_verdict(_intent(conf=0.7), gov_norm=gov_norm, governor_alive=True,
                            governor_holder="chevelle", policy=CRYPTO)
    assert v["allowed"] is True
    assert v["reason"] == "NO_GOVERNOR_DISSENT"
    expected = min(CRYPTO["MAX_UPWEIGHT"], max(CRYPTO["MAX_DOWNWEIGHT"], CRYPTO["MOMENTUM_WEIGHTING"]))
    assert v["risk_multiplier"] == pytest.approx(expected)


def test_stance_strings_treated_as_disagreement():
    # Stance string alone (no veto bit, no executable=False) still
    # signals disagreement when it's in the dissent set.
    for stance in ("VETO", "DISSENT", "RISK_DOWN", "HOLD", "REJECT", "ABSTAIN"):
        gov_norm = {"veto": False, "executable": None, "confidence": 0.6, "stance": stance}
        v = _governance_verdict(_intent(conf=0.9), gov_norm=gov_norm, governor_alive=True,
                                governor_holder="chevelle", policy=EQUITY)
        assert v["disagreement"] is True, f"stance {stance!r} should signal disagreement"
        # Should be soft dissent (high conf keeps it above floor).
        assert v["reason"] in {"SOFT_DISSENT_DOWNWEIGHTED", "SOFT_DISSENT_BELOW_FLOOR"}
