"""Tests for the unified arm surface (`/admin/trading/arm` + status).

Doctrine (2026-07-09 operator directive, iter-22):

    Prod has THREE master switches (env `BROKER_LIVE_ORDER_ENABLED`,
    Mongo `trading_controls.enabled`, Mongo `runtime_flags
    .master_trading_switch.enabled`). The env one showed LIVE while
    the two Mongo gates defaulted to CLOSED because no doc existed
    for them. Trader-sidecar receipts have been logging
    `master_switch_disarmed` on every cycle since 2026-07-02 as
    a result.

    The unified arm surface flips BOTH Mongo gates in one call so the
    trader can't be left half-armed. This suite asserts the write,
    the audit trail, the pre/post snapshotting, and the safety
    guards (reason required to arm; disarm is always allowed).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from db import db
from server import app


_MC_COLL = "trading_controls"
_MC_DOC = "current"
_MC_AUDIT = "trading_controls_audit"
_TR_DOC = "master_trading_switch"
_TR_COLL = "runtime_flags"


@pytest.fixture(autouse=True)
async def _clean_switch_docs():
    """The prod `test_database` MongoDB is shared with the live
    switches. Snapshot both docs on entry, purge, run test, restore.
    Same policy the earlier iterations use — synthetic cleanup so
    the operator's actual switch state is never disturbed."""
    mc_before = await db[_MC_COLL].find_one({"_id": _MC_DOC})
    tr_before = await db[_TR_COLL].find_one({"_id": _TR_DOC})
    # Purge for a clean slate.
    await db[_MC_COLL].delete_one({"_id": _MC_DOC})
    await db[_TR_COLL].delete_one({"_id": _TR_DOC})
    # Also purge any unified-arm audit rows this test will emit so
    # we don't leak `test-*` reason strings into the operator's tape.
    await db[_MC_AUDIT].delete_many(
        {"reason": {"$regex": "^test-unified-arm"}},
    )
    yield
    # Restore original state.
    await db[_MC_COLL].delete_one({"_id": _MC_DOC})
    await db[_TR_COLL].delete_one({"_id": _TR_DOC})
    if mc_before is not None:
        await db[_MC_COLL].insert_one(mc_before)
    if tr_before is not None:
        await db[_TR_COLL].insert_one(tr_before)
    await db[_MC_AUDIT].delete_many(
        {"reason": {"$regex": "^test-unified-arm"}},
    )


async def _login(client) -> str:
    """Grab an admin JWT for the seeded operator account."""
    r = await client.post(
        "/api/auth/login",
        json={
            "email": "admin@risedual.io",
            "password": "risedual-admin-2026",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("access_token") or body.get("token")


@pytest.mark.asyncio
async def test_arm_status_reflects_disarmed_defaults():
    """With BOTH Mongo docs missing, arm/status must show:
      mc_switch.enabled = False (seeded fail-closed)
      trader_switch.enabled = False (no doc default)
      all_armed = False
    The env layer state depends on the pod so we only check the
    Mongo-side fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.get(
            "/api/admin/trading/arm/status",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["mc_switch"]["enabled"] is False
    assert payload["trader_switch"]["enabled"] is False
    assert payload["all_armed"] is False
    assert payload["trader_will_fire"] is False


@pytest.mark.asyncio
async def test_arm_flips_both_docs_atomically():
    """POST /arm {enabled: True, reason: ...} writes BOTH switch
    docs, both showing enabled=True with the same actor + reason."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": True, "reason": "test-unified-arm-01"},
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["mc_switch"]["enabled"] is True
    assert payload["trader_switch"]["enabled"] is True
    assert payload["trader_will_fire"] is True

    # Verify BOTH Mongo docs were actually written (not just the
    # response payload).
    mc = await db[_MC_COLL].find_one({"_id": _MC_DOC})
    tr = await db[_TR_COLL].find_one({"_id": _TR_DOC})
    assert mc and mc.get("enabled") is True
    assert tr and tr.get("enabled") is True
    assert mc.get("reason") == "test-unified-arm-01"
    assert tr.get("reason") == "test-unified-arm-01"
    # Same actor stamped on both.
    assert mc.get("updated_by") == tr.get("updated_by")


@pytest.mark.asyncio
async def test_arm_without_reason_when_enabling_returns_400():
    """Enabling requires a reason — the audit-chain receipt. Missing
    reason must 400; the Mongo docs must NOT be flipped."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": True, "reason": "   "},
        )
    assert r.status_code == 400
    # Verify no writes leaked through.
    mc = await db[_MC_COLL].find_one({"_id": _MC_DOC})
    tr = await db[_TR_COLL].find_one({"_id": _TR_DOC})
    assert mc is None or mc.get("enabled") is False
    assert tr is None


@pytest.mark.asyncio
async def test_arm_disarm_does_not_require_reason():
    """Disarm is always allowed — reason optional. This is the
    emergency-stop path; blocking it on a reason field would defeat
    the whole point of a kill switch."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        # Arm first so there's something to disarm.
        await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": True, "reason": "test-unified-arm-02-warmup"},
        )
        # Now disarm with EMPTY reason.
        r = await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": False, "reason": ""},
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["mc_switch"]["enabled"] is False
    assert payload["trader_switch"]["enabled"] is False


@pytest.mark.asyncio
async def test_arm_writes_unified_audit_row():
    """Each unified arm/disarm writes ONE row into
    `trading_controls_audit` with `source="unified_arm"` and the
    pre/post state of BOTH switches so operators can trace which
    flip changed what."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": True, "reason": "test-unified-arm-03"},
        )
    row = await db[_MC_AUDIT].find_one(
        {"reason": "test-unified-arm-03", "source": "unified_arm"},
    )
    assert row is not None
    assert row["enabled"] is True
    assert row["pre_state"]["mc_switch"] is False
    assert row["pre_state"]["trader_switch"] is False
    assert row["post_state"]["mc_switch"] is True
    assert row["post_state"]["trader_switch"] is True


@pytest.mark.asyncio
async def test_arm_status_all_armed_only_when_env_and_both_mongo_true():
    """`all_armed` is true iff env_auto_router AND env_broker_live
    AND mc_switch AND trader_switch — surfacing the multi-layer
    gate topology so the operator can't be fooled by any single
    green light on the Flags page."""
    import os
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        # Arm both Mongo switches.
        await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": True, "reason": "test-unified-arm-04"},
        )
        r = await client.get(
            "/api/admin/trading/arm/status",
            headers={"Authorization": f"Bearer {tok}"},
        )
    payload = r.json()
    env_ar = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "true"
    env_bl = os.environ.get("BROKER_LIVE_ORDER_ENABLED", "false").lower() == "true"
    assert payload["mc_switch"]["enabled"] is True
    assert payload["trader_switch"]["enabled"] is True
    assert payload["env_auto_router"] is env_ar
    assert payload["env_broker_live"] is env_bl
    # `all_armed` must equal the AND of all four layers.
    assert payload["all_armed"] is (
        env_ar and env_bl and True and True
    )
