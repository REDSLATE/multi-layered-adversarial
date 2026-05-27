"""Tripwires: intent ingest persists market snapshot to gate-readable shape.

Doctrine pin (2026-02-18):
    The brain sidecars POST a `doctrine_snapshot` containing
    `spread_bps`, `price`, `volume`, etc. The gate chain reads from
    `intent.snapshot.spread_bps` (`shared/execution.py:235`). Both
    ingest paths (runtime-token and admin-proxy) MUST persist the
    doctrine_snapshot to the doc under the `snapshot` key.

    Pre-2026-02-18 this was silently dropped on the intent doc (only
    persisted to the `doctrine_sidecars` audit collection), which
    caused `roadguard_spread_floor` to fail closed at gate 7 with
    `ROADGUARD_MISSING_SPREAD_BPS — snapshot absent` on EVERY intent
    even when the brain dutifully sent the field. The Camaro team
    reported this as "MC needs a server-side broker adapter" — but
    the bridge existed, it just couldn't ever get past gate 7.
"""
from __future__ import annotations

import pytest
import requests


# ─── Admin-proxy ingest ───────────────────────────────────────────────


@pytest.mark.tripwire
async def test_admin_proxy_persists_snapshot_to_intent_doc(auth_client, base_url):
    """POST an intent with a `doctrine_snapshot` and assert the
    persisted intent doc carries it as `snapshot` with `spread_bps`
    intact. Reads back via Mongo directly (no runtime-token dance)."""
    from db import db
    body = {
        "stack": "camaro",
        "symbol": "TRIPWIRE_SPREAD_A",
        "action": "BUY",
        "confidence": 0.75,
        "lane": "equity",
        "rationale": "tripwire: admin proxy snapshot persistence",
        "doctrine_snapshot": {
            "spread_bps": 4.5,
            "price": 187.50,
            "volume": 1_000_000,
            "relative_volume": 2.1,
        },
    }
    r = auth_client.post(f"{base_url}/api/admin/intents", json=body, timeout=15)
    assert r.status_code == 200, r.text
    intent_id = r.json()["intent_id"]

    intent = await db["shared_intents"].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    assert intent is not None, "ingested intent not found in DB"
    snap = intent.get("snapshot") or {}
    assert snap.get("spread_bps") == 4.5, (
        f"intent.snapshot.spread_bps MUST be persisted; got {snap!r}"
    )
    assert snap.get("price") == 187.50


@pytest.mark.tripwire
async def test_admin_proxy_handles_missing_snapshot_as_empty_dict(auth_client, base_url):
    """When the brain doesn't send a snapshot, the intent doc must
    persist `snapshot={}` (NOT None) so downstream readers can
    `.get('spread_bps')` without crashing."""
    from db import db
    body = {
        "stack": "camaro",
        "symbol": "TRIPWIRE_SPREAD_B",
        "action": "BUY",
        "confidence": 0.75,
        "lane": "equity",
        "rationale": "tripwire: admin proxy missing-snapshot",
    }
    r = auth_client.post(f"{base_url}/api/admin/intents", json=body, timeout=15)
    assert r.status_code == 200, r.text
    intent_id = r.json()["intent_id"]
    intent = await db["shared_intents"].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    assert intent is not None
    snap = intent.get("snapshot")
    # Doctrine pin (2026-05-27): auto-dry-run injects a sentinel
    # `spread_bps=9999.0` + `spread_source="sentinel_unknown"` when
    # the brain omits the doctrine_snapshot, so RoadGuard fails-closed
    # with a clear reason instead of crashing on missing key. The
    # intent doc must still expose those sentinel fields under
    # `snapshot` so downstream `.get('spread_bps')` callers work.
    assert isinstance(snap, dict), (
        f"missing snapshot must persist as a dict; got {snap!r}"
    )
    assert snap.get("spread_bps") == 9999.0, (
        f"missing snapshot must carry sentinel spread_bps=9999.0; got {snap!r}"
    )
    assert snap.get("spread_source") == "sentinel_unknown", (
        f"missing snapshot must carry spread_source=sentinel_unknown; got {snap!r}"
    )


# ─── Gate-chain end-to-end ────────────────────────────────────────────


@pytest.mark.tripwire
async def test_gate_chain_reads_persisted_snapshot_after_admin_ingest(auth_client, base_url):
    """The whole point: after ingest, the gate chain MUST be able to
    read `spread_bps` from the intent doc and pass `roadguard_spread_floor`.
    """
    from db import db
    from shared.execution import _evaluate_gates

    # Fresh ingest with a healthy spread.
    body = {
        "stack": "camaro",
        "symbol": "TRIPWIRE_GATE_CHAIN",
        "action": "BUY",
        "confidence": 0.78,
        "lane": "equity",
        "rationale": "tripwire: gate chain reads persisted snapshot",
        "doctrine_snapshot": {"spread_bps": 5.0, "price": 100.0, "volume": 500_000},
    }
    r = auth_client.post(f"{base_url}/api/admin/intents", json=body, timeout=15)
    assert r.status_code == 200, r.text
    intent_id = r.json()["intent_id"]

    intent = await db["shared_intents"].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    assert intent is not None
    result = await _evaluate_gates(intent, 100.0)
    gates = {g["name"]: g for g in result["gates"]}
    rg = gates["roadguard_spread_floor"]
    assert rg["passed"] is True, (
        f"roadguard_spread_floor MUST pass when spread_bps is "
        f"persisted; got reason={rg['reason']!r}"
    )


@pytest.mark.tripwire
async def test_gate_chain_fails_roadguard_on_wide_spread():
    """Sanity: the gate still fails on actually-wide spreads. The fix
    must not loosen RoadGuard's intent — only ensure it sees the data."""
    from shared.execution import _evaluate_gates
    # Construct a directly-built intent dict to test the gate in
    # isolation. `lane=equity` cap is 50 bps; 80 must fail.
    intent = {
        "intent_id": "tripwire-roadguard-wide",
        "stack": "camaro", "symbol": "WIDE_SPREAD_TEST",
        "action": "BUY", "lane": "equity",
        "may_execute": False, "requires_gate_pass": True,
        "holds_executor_seat": True, "executor_holder_at_post": "camaro",
        "confidence": 0.75,
        "snapshot": {"spread_bps": 80.0},
    }
    result = await _evaluate_gates(intent, 100.0)
    gates = {g["name"]: g for g in result["gates"]}
    rg = gates["roadguard_spread_floor"]
    assert rg["passed"] is False
    assert "ROADGUARD_SPREAD_CAP" in rg["reason"]
