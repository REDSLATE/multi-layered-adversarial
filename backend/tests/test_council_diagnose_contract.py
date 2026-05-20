"""Drift tripwire for the live `/api/admin/execution/diagnose` contract.

After the 2026-05-17 council refactor, the diagnose endpoint became the
operator's single window into "what's blocking live trading?". This
test pins the SHAPE of that contract — gate names, gate ordering, the
keys every council gate must surface, the broker-status block, and the
top-level response keys.

If this test fails, the council behavior changed. EITHER:
  (a) The change is intentional → update this fixture to match the new
      contract AND drop a line in /app/memory/PRD.md so the next session
      knows the diagnose surface shifted.
  (b) The change is unintentional → roll the offending edit back.

The test deliberately does NOT pin volatile numeric values (timestamps,
quantum regime probabilities, risk_multiplier post-quantum). Only shape.
"""
from __future__ import annotations

import os

import pytest
import requests


# Tripwire suite: live HTTP contract pin for /api/admin/execution/diagnose.
# See pytest.ini for the marker definition.
pytestmark = pytest.mark.tripwire


BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://b177ffdc-73ff-45fb-9ba4-f1e63e5e4274.preview.emergentagent.com",
).rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASS = "risedual-admin-2026"


# ── Expected contract — bump intentionally when behavior changes. ──────

EXPECTED_GATES_IN_ORDER = (
    "schema_invariants",
    "action_routable",
    "executor_seat_check",
    "live_trading_disabled",
    "broker_connected",
    # Doctrine (c, 2026-05-20): RoadGuard owns deterministic market-
    # structure safety and runs BEFORE council. Governor sizes within
    # the safe zone; RoadGuard kills if structure itself is unsafe.
    "roadguard_spread_floor",
    "governor_authority",
    "opponent_objection",
    # Cap rows vary by lane — equity gets `cap_per_order`; crypto gets
    # `cap_per_order_crypto`. Both also append `cap_per_day` and
    # `cap_open_notional`. Checked separately below to keep this list
    # lane-neutral.
)

CAP_GATES_EQUITY = ("cap_per_order", "cap_per_day", "cap_open_notional")
CAP_GATES_CRYPTO = ("cap_per_order_crypto", "cap_per_day", "cap_open_notional")

# Council gates must carry these keys regardless of verdict.
GOVERNOR_GATE_REQUIRED_KEYS = {
    "name", "passed", "reason", "verdict_code", "disagreement",
    "risk_multiplier", "effective_conf", "lane", "policy_used",
    "quantum_state",
}
OPPONENT_GATE_REQUIRED_KEYS = {
    "name", "passed", "reason", "opponent_holder", "opponent_conf",
    "opponent_side", "opponent_opposes",
}

# Top-level diagnose response keys.
TOP_LEVEL_REQUIRED_KEYS = {
    "lane", "sample_symbol", "synthetic_notional_usd", "synthetic_intent",
    "verdict", "first_blocker", "gates", "broker", "caps",
    "risk_multiplier", "checked_at",
}

# Verdict codes the council may emit. Adding a new verdict code is a
# semantic change to the contract — bump this set + the test that
# pins it intentionally.
KNOWN_VERDICT_CODES = {
    "GOVERNOR_SEAT_VACANT",
    "GOVERNOR_OFFLINE",
    "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT",
    "NO_STANCE_LOW_EFFECTIVE_CONF",
    "GOVERNOR_HARD_VETO",
    "SOFT_DISSENT_DOWNWEIGHTED",
    "SOFT_DISSENT_BELOW_FLOOR",
    "NO_GOVERNOR_DISSENT",
}


@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token")
    assert tok, "login did not return access_token"
    return tok


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fetch_diagnose(headers: dict, lane: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/api/admin/execution/diagnose",
        params={"lane": lane, "notional_usd": 25},
        headers=headers,
        timeout=20,
    )
    assert r.status_code == 200, f"diagnose({lane}) returned {r.status_code}: {r.text}"
    return r.json()


# ── Shape pins ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("lane", ["crypto", "equity"])
def test_diagnose_top_level_shape(headers, lane):
    d = _fetch_diagnose(headers, lane)
    missing = TOP_LEVEL_REQUIRED_KEYS - set(d.keys())
    assert not missing, f"diagnose({lane}) is missing top-level keys: {missing}"
    assert d["lane"] == lane
    assert d["verdict"] in {"would_pass", "would_block"}


@pytest.mark.parametrize("lane,expected_caps", [
    ("crypto", CAP_GATES_CRYPTO),
    ("equity", CAP_GATES_EQUITY),
])
def test_diagnose_gate_chain_ordering(headers, lane, expected_caps):
    """Gate names must appear in the canonical chain order. New gates
    inserted out-of-order are a SEMANTIC change to the chain and need
    explicit operator review."""
    d = _fetch_diagnose(headers, lane)
    gate_names = [g["name"] for g in d["gates"]]
    # Core sequence first.
    for i, expected in enumerate(EXPECTED_GATES_IN_ORDER):
        assert gate_names[i] == expected, (
            f"gate[{i}] expected {expected!r}, got {gate_names[i]!r} "
            f"(full chain: {gate_names})"
        )
    # Cap rows must follow the council, in the lane-correct order.
    cap_section = gate_names[len(EXPECTED_GATES_IN_ORDER):]
    assert tuple(cap_section) == expected_caps, (
        f"cap gates for lane={lane!r} expected {expected_caps}, got {cap_section}"
    )


@pytest.mark.parametrize("lane", ["crypto", "equity"])
def test_diagnose_council_gate_required_keys(headers, lane):
    """Governor + opponent gate rows must surface their full advisory
    payload. Renaming or dropping any of these keys breaks the
    Diagnostics UI's LiveTradeDiagnose panel."""
    d = _fetch_diagnose(headers, lane)
    gates_by_name = {g["name"]: g for g in d["gates"]}
    gov = gates_by_name["governor_authority"]
    opp = gates_by_name["opponent_objection"]
    gov_missing = GOVERNOR_GATE_REQUIRED_KEYS - set(gov.keys())
    opp_missing = OPPONENT_GATE_REQUIRED_KEYS - set(opp.keys())
    assert not gov_missing, f"governor_authority missing keys: {gov_missing}"
    assert not opp_missing, f"opponent_objection missing keys: {opp_missing}"
    # The verdict code must be a known code, not an arbitrary string.
    assert gov["verdict_code"] in KNOWN_VERDICT_CODES, (
        f"governor verdict_code {gov['verdict_code']!r} is not in the "
        f"known set — if this is a new code, append it to "
        f"KNOWN_VERDICT_CODES and document it in PRD.md."
    )


@pytest.mark.parametrize("lane", ["crypto", "equity"])
def test_diagnose_quantum_state_shape(headers, lane):
    """The quantum-inspired overlay must always surface its regime
    distribution + risk multiplier on the governor gate. Refactors of
    `_apply_quantum_overlay` must preserve this contract."""
    d = _fetch_diagnose(headers, lane)
    gov = next(g for g in d["gates"] if g["name"] == "governor_authority")
    qs = gov.get("quantum_state") or {}
    assert "regime_probs" in qs, "quantum_state must expose regime_probs"
    assert "risk_multiplier" in qs, "quantum_state must expose risk_multiplier"
    assert "entropy" in qs, "quantum_state must expose entropy"
    # regime_probs sums to ~1.0 (probability distribution invariant).
    total = sum(float(v) for v in (qs["regime_probs"] or {}).values())
    assert abs(total - 1.0) < 0.01, (
        f"regime_probs must sum to ~1.0, got {total:.4f}"
    )


@pytest.mark.parametrize("lane", ["crypto", "equity"])
def test_diagnose_broker_status_shape(headers, lane):
    """Broker block must always carry lane + adapter_loaded. Crypto
    additionally surfaces kraken_credentials.state (the diagnostic
    fingerprint the operator uses to debug PROD encryption-key drift)."""
    d = _fetch_diagnose(headers, lane)
    broker = d["broker"]
    assert broker["lane"] == lane
    assert "adapter_loaded" in broker
    if lane == "crypto":
        kc = broker.get("kraken_credentials") or {}
        assert "state" in kc, "crypto diagnose must surface kraken_credentials.state"
        assert kc["state"] in {"ok", "no_credentials", "missing_field", "decrypt_failed"}, (
            f"unexpected kraken_credentials.state: {kc['state']!r}"
        )


def test_diagnose_first_blocker_consistency(headers):
    """When verdict=would_block, first_blocker MUST be the first failing
    gate. When verdict=would_pass, first_blocker MUST be null."""
    for lane in ("crypto", "equity"):
        d = _fetch_diagnose(headers, lane)
        if d["verdict"] == "would_block":
            assert d["first_blocker"] is not None
            failing = [g for g in d["gates"] if not g["passed"]]
            assert failing, "would_block but no failing gates?"
            assert d["first_blocker"]["name"] == failing[0]["name"]
        else:
            assert d["first_blocker"] is None
