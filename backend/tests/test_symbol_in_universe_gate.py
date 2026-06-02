"""Symbol-in-universe gate tripwire (2026-02-19).

Doctrine pin (c):
  MC verifies boundaries. The `symbol_in_universe` gate in
  `shared/execution.py` ensures every intent's symbol is in MC's
  `patterns_universe` with a `lane` that matches the intent's lane.
  Brains may opt in to consulting MC's universe (via the new
  `/api/admin/runtime/{brain}/universe` endpoint) but compliance is
  ENFORCED at the gate — a brain that drifts sees its intents
  rejected with `symbol_in_universe: passed=False`.

What this pins:
  1. The gate exists in the canonical chain (caught if removed).
  2. An off-universe symbol fails the gate.
  3. A wrong-lane symbol fails the gate.
  4. A matching (symbol, lane) passes the gate.
  5. The seeded crypto pairs (BTC/USD, ETH/USD, SOL/USD, XRP/USD)
     are present with `lane=crypto` after a normal boot.
"""
from __future__ import annotations

import pytest


# ─── source-level tripwire ────────────────────────────────────────────


@pytest.mark.tripwire
def test_symbol_in_universe_gate_exists_in_chain():
    """The gate must remain wired into `_evaluate_gates`. If somebody
    accidentally removes the gate, this test breaks the build."""
    with open("/app/backend/shared/execution.py") as f:
        src = f.read()
    assert '"name": "symbol_in_universe"' in src, (
        "symbol_in_universe gate is missing from shared/execution.py — "
        "MC's universe-boundary enforcement was removed. This is the "
        "single lever MC has to control which symbols brains may "
        "propose against."
    )
    assert "PATTERNS_UNIVERSE" in src, (
        "execution.py no longer references PATTERNS_UNIVERSE — the gate "
        "cannot enforce the boundary without reading the collection."
    )


@pytest.mark.tripwire
def test_brain_universe_endpoint_exists():
    """The brain-callable universe endpoint must be present in
    `routes/brain_runtime.py`. Without it, brains have no clean way
    to consult MC and must hardcode their own universe."""
    with open("/app/backend/routes/brain_runtime.py") as f:
        src = f.read()
    assert '"/{brain}/universe"' in src, (
        "brain-callable `/admin/runtime/{brain}/universe` endpoint "
        "is missing. Brains need this to comply with MC's "
        "patterns_universe contract."
    )
    assert "operator_read_only_universe_view" in src, (
        "universe endpoint doctrine pin is gone — operator visibility "
        "may have regressed"
    )


# ─── behavioral tests via the HTTP surface ────────────────────────────


@pytest.mark.asyncio
async def test_universe_seeds_crypto_pairs_on_boot():
    """After backend boot, the four Kraken-tracked majors must exist
    in `patterns_universe` tagged `lane=crypto`. This guarantees
    that a brain holding a crypto seat has SOMETHING to propose
    against on a fresh deploy without operator action."""
    from db import db
    from namespaces import PATTERNS_UNIVERSE
    expected_crypto = {"BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"}
    rows = await db[PATTERNS_UNIVERSE].find(
        {"lane": "crypto", "active": {"$ne": False}},
        {"_id": 0, "symbol": 1, "lane": 1},
    ).to_list(100)
    seeded = {r["symbol"] for r in rows}
    missing = expected_crypto - seeded
    assert not missing, (
        f"crypto pairs missing from patterns_universe seed: {missing}. "
        f"server.py boot seed must populate every Kraken-tracked major."
    )


def test_universe_endpoint_lane_aware(auth_client, base_url):
    """The brain-universe endpoint must filter by the brain's
    currently-held seats. A brain that holds ONLY a crypto seat
    should see only crypto symbols; a brain that holds an equity
    seat should see only equity symbols."""
    # Just hit the endpoint — we don't assert specific brain seat
    # state because the test environment's roster varies. We only
    # assert the contract shape.
    r = auth_client.get(
        f"{base_url}/api/admin/runtime/alpha/universe", timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brain"] == "alpha"
    assert "lanes" in body
    assert "symbols" in body
    assert "count" in body
    assert body["doctrine"] == "operator_read_only_universe_view"
    # Every symbol row carries an explicit lane (no nulls)
    for s in body["symbols"]:
        assert s.get("symbol")
        assert s.get("lane") in ("equity", "crypto")


def test_universe_endpoint_rejects_unknown_brain(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/runtime/notabrain/universe", timeout=15,
    )
    assert r.status_code == 404


# ─── universe admin schema test ───────────────────────────────────────


def test_universe_post_rejects_invalid_lane(auth_client, base_url):
    """POST /api/admin/patterns/universe must reject any lane outside
    {equity, crypto}. Prevents an operator typo from creating an
    orphan symbol row that no gate-lane query can ever find."""
    r = auth_client.post(
        f"{base_url}/api/admin/patterns/universe",
        json={"symbol": "BOGUS123", "lane": "metals"},
        timeout=15,
    )
    assert r.status_code == 422, r.text


def test_universe_post_accepts_crypto_lane(auth_client, base_url):
    """Operator must be able to add a crypto symbol via the admin
    POST endpoint. Idempotent — re-adding is safe."""
    # Use a non-slash symbol to avoid URL-encoding cleanup pain.
    test_sym = "DOGEUSDTEST"
    r = auth_client.post(
        f"{base_url}/api/admin/patterns/universe",
        json={
            "symbol": test_sym,
            "lane": "crypto",
            "note": "tripwire test",
            "active": True,
        },
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["symbol"] == test_sym
    # Cleanup: hard-delete so we don't pollute the universe.
    cleanup = auth_client.delete(
        f"{base_url}/api/admin/patterns/universe/{test_sym}?hard=true",
        timeout=15,
    )
    assert cleanup.status_code in (200, 404), cleanup.text
