"""Snapshot contract — drift tripwire + public endpoint contract.

This is the load-bearing test that catches the failure mode where MC
silently updates the snapshot field list but brains keep shipping the
old shape. Same pattern as `test_mc_checkin_policy_hash.py` on the
brain side.

If MC updates `shared/calibration/snapshot_contract.py`, this test
fails with a clear "bump KNOWN_HASH AND notify every brain agent"
message, forcing the operator to acknowledge the doctrine change.
"""
from __future__ import annotations

import requests

from shared.calibration.snapshot_contract import (
    SNAPSHOT_KEYS_FULL_CRYPTO,
    SNAPSHOT_KEYS_FULL_EQUITY,
    SNAPSHOT_KEYS_MINIMUM,
    SPREAD_BPS_UNKNOWN,
    compute_spread_bps,
    contract_hash,
    contract_payload,
)


# ───── KNOWN HASH lock-in ─────────────────────────────────────────────
#
# As of 2026-02-19, MC's snapshot contract sha256 is this value.
# Bumping this requires:
#   1. Edit `shared/calibration/snapshot_contract.py` (the keys / sentinel)
#   2. Update KNOWN_HASH below to the new contract_hash() output
#   3. Notify EVERY brain agent — they must re-fetch from
#      `GET /api/runtime/survival/snapshot-contract` and bump their own
#      local copy of the contract.
#
# DO NOT bump this without doing step 3. Silent drift is the bug this
# test exists to prevent.

CONTRACT_KNOWN_HASH = "1214e673813f00a827fa1b9635511ea22bc787d0a1280a807f0b48eeea0d6184"


def test_contract_hash_is_locked_in():
    """If this fails, MC's snapshot contract has changed. Update
    CONTRACT_KNOWN_HASH above AND notify every brain agent. Silent
    drift = brains shipping stale snapshots = sentinel-driven REJECTs."""
    actual = contract_hash()
    assert actual == CONTRACT_KNOWN_HASH, (
        f"\n  SNAPSHOT CONTRACT HASH DRIFT\n"
        f"  computed: {actual}\n"
        f"  expected: {CONTRACT_KNOWN_HASH}\n"
        f"  If this change is intentional:\n"
        f"    1. Bump CONTRACT_KNOWN_HASH in this test\n"
        f"    2. Notify every brain agent to re-fetch from\n"
        f"       /api/runtime/survival/snapshot-contract\n"
        f"    3. Update each brain's local copy of the field list\n"
        f"  If this change is NOT intentional: revert your edit to\n"
        f"  `shared/calibration/snapshot_contract.py`."
    )


# ───── MINIMUM tier — alignment with Alpha's intent_enrichment.py ─────


def test_minimum_keys_are_alphas_seven():
    """Alpha's `services/intent_enrichment.py:SNAPSHOT_KEYS` defined
    these seven names. MC's MINIMUM must match byte-for-byte so brains
    importing either contract get the same shape."""
    assert SNAPSHOT_KEYS_MINIMUM == (
        "bid",
        "ask",
        "spread_bps",
        "volume_24h_usd",
        "volatility_1h",
        "trend_strength",
        "exchange_liquidity_score",
    )


def test_minimum_is_subset_of_full_crypto():
    """The MINIMUM tier must be a strict subset of FULL_CRYPTO so a
    brain that ships only the 7 still satisfies the MINIMUM check."""
    assert set(SNAPSHOT_KEYS_MINIMUM).issubset(set(SNAPSHOT_KEYS_FULL_CRYPTO))


def test_minimum_overlaps_full_equity_on_execution_fields():
    """Equity doctrine uses different vocabulary, but at least the
    execution-grade fields (bid/ask/spread_bps/volume_24h_usd) overlap."""
    overlap = set(SNAPSHOT_KEYS_MINIMUM) & set(SNAPSHOT_KEYS_FULL_EQUITY)
    assert {"bid", "ask", "spread_bps", "volume_24h_usd"}.issubset(overlap)


# ───── compute_spread_bps — semantic match with Alpha's helper ────────


def test_compute_spread_bps_canonical_math():
    # 67253.10 ask vs 67250.50 bid → mid 67251.80 → 2.6/67251.8*10000
    # ≈ 0.39 bps
    result = compute_spread_bps(67250.50, 67253.10)
    assert abs(result - 0.39) < 0.01


def test_compute_spread_bps_returns_unknown_on_zero():
    assert compute_spread_bps(0, 100) == SPREAD_BPS_UNKNOWN
    assert compute_spread_bps(100, 0) == SPREAD_BPS_UNKNOWN
    assert compute_spread_bps(0, 0) == SPREAD_BPS_UNKNOWN


def test_compute_spread_bps_returns_unknown_on_garbage():
    assert compute_spread_bps("abc", "def") == SPREAD_BPS_UNKNOWN
    assert compute_spread_bps(None, None) == SPREAD_BPS_UNKNOWN
    assert compute_spread_bps(float("inf"), 100) == SPREAD_BPS_UNKNOWN
    assert compute_spread_bps(float("nan"), 100) == SPREAD_BPS_UNKNOWN


def test_compute_spread_bps_does_not_raise():
    """No input — no matter how malformed — may raise. The brain's
    snapshot path must never crash on a spread calc."""
    for inputs in [
        (None, None), ("", ""), ({}, {}), ([], []), (object(), object()),
    ]:
        try:
            compute_spread_bps(*inputs)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"compute_spread_bps raised on {inputs}: {e}")


# ───── public endpoint contract ───────────────────────────────────────


def test_public_endpoint_returns_contract_payload(base_url):
    """No auth required — doctrine-pinned read, same as
    /api/runtime/survival/policy-hash. Brains hit this at boot to
    confirm they're using MC's current contract."""
    r = requests.get(
        f"{base_url}/api/runtime/survival/snapshot-contract",
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for k in [
        "contract_hash",
        "spread_bps_unknown",
        "minimum_keys",
        "full_crypto_keys",
        "full_equity_keys",
        "doctrine",
    ]:
        assert k in body, f"missing key in public response: {k}"


def test_public_endpoint_no_auth_required(base_url):
    """The contract is doctrine-pinned read — no JWT required. If this
    starts returning 401/403, the auth gate has rotted."""
    r = requests.get(
        f"{base_url}/api/runtime/survival/snapshot-contract",
        timeout=15,
    )
    assert r.status_code == 200


def test_public_endpoint_hash_matches_local_contract(base_url):
    """The hash MC serves must equal the hash MC computes locally —
    proof that the HTTP layer isn't out of sync with the constants."""
    r = requests.get(
        f"{base_url}/api/runtime/survival/snapshot-contract",
        timeout=15,
    )
    assert r.json()["contract_hash"] == contract_hash()


def test_contract_payload_matches_public_endpoint(base_url):
    """The helper used internally must produce the same shape as the
    public endpoint. (Catches the case where the HTTP layer reshapes
    the payload differently from the library helper.)"""
    r = requests.get(
        f"{base_url}/api/runtime/survival/snapshot-contract",
        timeout=15,
    )
    expected = contract_payload()
    assert r.json() == expected


# ───── snapshot-completeness diagnostic surfaces the hash ─────────────


def test_diagnostic_carries_contract_hash(auth_client, base_url):
    """The admin diagnostic must surface the same hash the public
    endpoint serves — so an operator looking at the dashboard can
    verify "brains are talking to the MC version I think they are."""
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=24",
        timeout=20,
    )
    assert r.json()["snapshot_contract_hash"] == contract_hash()


def test_diagnostic_returns_tier_keys(auth_client, base_url):
    """The diagnostic must publish the same tier keys the public
    contract endpoint publishes — single source of truth."""
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=24",
        timeout=20,
    )
    body = r.json()
    assert tuple(body["tier_keys"]["minimum"]) == SNAPSHOT_KEYS_MINIMUM
    assert tuple(body["tier_keys"]["full_crypto"]) == SNAPSHOT_KEYS_FULL_CRYPTO
    assert tuple(body["tier_keys"]["full_equity"]) == SNAPSHOT_KEYS_FULL_EQUITY


def test_per_brain_tiers_present(auth_client, base_url):
    """Every brain row must carry the tiered breakdown so the operator
    sees minimum vs full completeness at a glance."""
    r = auth_client.get(
        f"{base_url}/api/admin/intents/snapshot-completeness?hours=168",
        timeout=20,
    )
    body = r.json()
    for brain, blk in body["by_brain"].items():
        assert "tiers" in blk, f"brain {brain} missing tiers block"
        for tier_name in ("minimum", "full_crypto", "full_equity"):
            assert tier_name in blk["tiers"], (
                f"brain {brain} missing tier {tier_name}"
            )
            for k in ("intents", "average_completeness", "fully_complete"):
                assert k in blk["tiers"][tier_name]
