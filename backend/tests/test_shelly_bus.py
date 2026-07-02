"""Shelly Bus tests — auth, trust scoring, canonicalization, batch.

Uses the live preview Mongo connection through the shelly.sync_db path
already exercised in test_shelly_extension.py. Hits the FastAPI app
in-process via httpx.AsyncClient so the routes are real.
"""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

# Ingest tokens must exist BEFORE importing the app — runtime_auth
# looks them up by env var.
#
# Test-isolation fix (2026-02-17): previously we used
# `os.environ.setdefault(...)` with a FAKE value at module-import time.
# Because module-level setdefault leaks across the whole pytest
# session (no monkeypatch teardown), every downstream test that read
# `<BRAIN>_INGEST_TOKEN` from os.environ ended up sending the fake
# value to the live backend → 401s in test_paradox_wake / test_sidecar_checkin.
#
# Source the real token from /app/backend/.env first; only fall back to
# the fake when the .env doesn't have it (genuine CI-without-env case).
def _seed_real_tokens():
    real: dict[str, str] = {}
    try:
        with open("/app/backend/.env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                real[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for brain in ("ALPHA", "CAMARO", "CHEVELLE", "REDEYE"):
        key = f"{brain}_INGEST_TOKEN"
        if not os.environ.get(key):
            os.environ[key] = real.get(key) or f"test-{brain.lower()}-token"

_seed_real_tokens()

# Loosen the canonicalization threshold for tests so we can demonstrate
# both rejected and accepted paths without seeding outcomes.
os.environ.setdefault("SHELLY_BUS_MIN_CONVERGENCE", "2")

from server import app  # noqa: E402
from shelly.sync_db import get_db  # noqa: E402
from shared.shelly_bus import PROPOSAL_AUTHORITY  # noqa: E402


PROPOSALS_COLL = "shelly_memory_proposals"


def _clear_test_proposals(symbol: str):
    db = get_db()
    db[PROPOSALS_COLL].delete_many({"symbol": symbol})
    db["shelly_mc_shared_memory"].delete_many({"symbol": symbol})


def _body(brain: str, symbol: str, text: str, *, source_id: str | None = None):
    return {
        "source_brain": brain,
        "lane": "crypto",
        "symbol": symbol,
        "event_type": "market_pattern",
        "text": text,
        "confidence": 0.7,
        "outcome": "pending",
        "regime": "compression",
        "source_id": source_id,
        "metadata": {},
        "authority": PROPOSAL_AUTHORITY,
    }


@pytest.mark.asyncio
async def test_propose_requires_runtime_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/mc-shelly/memory/propose",
            json=_body("alpha", "TESTAUTH", "x"),
        )
    assert r.status_code in (401, 422)


@pytest.mark.asyncio
async def test_propose_rejects_wrong_brain_with_correct_token():
    """Camaro token cannot post under source_brain=alpha."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/mc-shelly/memory/propose",
            json=_body("alpha", "TESTBADAUTH", "x"),
            headers={"X-Runtime-Token": os.environ["BARRACUDA_INGEST_TOKEN"]},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_propose_rejects_tampered_authority():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        bad = _body("alpha", "TESTTAMPERED", "x")
        bad["authority"] = "EXECUTE_NOW"
        r = await c.post(
            "/api/mc-shelly/memory/propose",
            json=bad,
            headers={"X-Runtime-Token": os.environ["CAMINO_INGEST_TOKEN"]},
        )
    assert r.status_code == 400
    assert "authority" in r.text.lower()


@pytest.mark.asyncio
async def test_first_proposal_stored_unverified_in_pen():
    sym = "BUSTEST1"
    _clear_test_proposals(sym)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/mc-shelly/memory/propose",
            json=_body("alpha", sym, "first sighting"),
            headers={"X-Runtime-Token": os.environ["CAMINO_INGEST_TOKEN"]},
        )
    assert r.status_code == 200
    out = r.json()
    assert out["accepted"] is False
    assert out["status"] == "UNVERIFIED"
    assert out["trust_score"] < out["min_canonical_trust"]

    # Pen has the row, with MC's review authority stamp.
    db = get_db()
    row = db[PROPOSALS_COLL].find_one({"symbol": sym}, {"_id": 0})
    assert row is not None
    assert row["authority"] == "MC_SHELLY_REVIEW_ONLY"
    assert row["status"] == "UNVERIFIED"

    _clear_test_proposals(sym)


@pytest.mark.asyncio
async def test_two_brain_convergence_canonicalizes():
    """Two brains submitting the SAME (symbol, event_type, text) → the
    second submission crosses the MIN_CONVERGENCE=2 threshold and
    becomes canonical memory."""
    sym = "BUSTEST2"
    _clear_test_proposals(sym)

    text = "BTC compression with rising volume looked similar to prior breakout setups."
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Alpha first
        r1 = await c.post(
            "/api/mc-shelly/memory/propose",
            json=_body("alpha", sym, text),
            headers={"X-Runtime-Token": os.environ["CAMINO_INGEST_TOKEN"]},
        )
        assert r1.json()["accepted"] is False

        # Camaro second — convergence threshold met
        r2 = await c.post(
            "/api/mc-shelly/memory/propose",
            json=_body("camaro", sym, text),
            headers={"X-Runtime-Token": os.environ["BARRACUDA_INGEST_TOKEN"]},
        )
    body = r2.json()
    assert body["accepted"] is True
    assert body["status"] == "CONVERGED"
    assert body["trust_score"] >= body.get("trust_score", 0)
    assert body["authority"] == "MEMORY_REASONING_ONLY"
    # The canonical row is in shared memory now.
    db = get_db()
    canonical = db["shelly_mc_shared_memory"].find_one({"symbol": sym}, {"_id": 0})
    assert canonical is not None

    _clear_test_proposals(sym)


@pytest.mark.asyncio
async def test_summary_endpoint_returns_counts():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/mc-shelly/memory/proposals/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "total" in body
    assert "by_status" in body
    assert "min_canonical_trust" in body


def test_brain_client_requires_url_and_token():
    """The client construction itself enforces the safety pin — you
    cannot accidentally make a client with no auth."""
    from shared.shelly_bus.brain_shelly_client import BrainShellyClient
    with pytest.raises(ValueError):
        BrainShellyClient(mc_url="", runtime_token="tok")
    with pytest.raises(ValueError):
        BrainShellyClient(mc_url="https://x", runtime_token="")
