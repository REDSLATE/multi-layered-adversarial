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
_LN_DOC = "lane_enabled"


@pytest.fixture(autouse=True)
async def _clean_switch_docs():
    """The prod `test_database` MongoDB is shared with the live
    switches. Snapshot the three docs on entry, purge, run test,
    restore. Same policy the earlier iterations use — synthetic
    cleanup so the operator's actual switch state is never disturbed."""
    mc_before = await db[_MC_COLL].find_one({"_id": _MC_DOC})
    tr_before = await db[_TR_COLL].find_one({"_id": _TR_DOC})
    ln_before = await db[_TR_COLL].find_one({"_id": _LN_DOC})
    # Purge for a clean slate.
    await db[_MC_COLL].delete_one({"_id": _MC_DOC})
    await db[_TR_COLL].delete_one({"_id": _TR_DOC})
    await db[_TR_COLL].delete_one({"_id": _LN_DOC})
    # Also purge any unified-arm audit rows this test will emit so
    # we don't leak `test-*` reason strings into the operator's tape.
    await db[_MC_AUDIT].delete_many(
        {"reason": {"$regex": "^test-unified-arm"}},
    )
    yield
    # Restore original state.
    await db[_MC_COLL].delete_one({"_id": _MC_DOC})
    await db[_TR_COLL].delete_one({"_id": _TR_DOC})
    await db[_TR_COLL].delete_one({"_id": _LN_DOC})
    if mc_before is not None:
        await db[_MC_COLL].insert_one(mc_before)
    if tr_before is not None:
        await db[_TR_COLL].insert_one(tr_before)
    if ln_before is not None:
        await db[_TR_COLL].insert_one(ln_before)
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
    AND mc_switch AND trader_switch AND at least one lane on —
    surfacing the multi-layer gate topology so the operator can't be
    fooled by any single green light on the Flags page."""
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
    # Lanes default to True when doc is missing.
    assert payload["lanes"]["equity"]["enabled"] is True
    assert payload["lanes"]["crypto"]["enabled"] is True
    # `all_armed` must equal env_ar AND env_bl AND all downstream
    # switches (both master + at least one lane).
    assert payload["all_armed"] is (env_ar and env_bl)


# ─── Per-lane control (2026-07-09 operator refinement) ──────────

@pytest.mark.asyncio
async def test_arm_with_lanes_updates_both_master_and_lane_doc():
    """Passing a `lanes` map alongside `enabled` writes the arm docs
    AND the lane_enabled doc in one round trip. Operator can say
    "arm master but keep crypto off" with one call."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "enabled": True,
                "reason": "test-unified-arm-05-equity-only",
                "lanes": {"equity": True, "crypto": False},
            },
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["mc_switch"]["enabled"] is True
    assert payload["trader_switch"]["enabled"] is True
    assert payload["lanes"]["equity"]["enabled"] is True
    assert payload["lanes"]["crypto"]["enabled"] is False
    assert payload["lanes"]["equity"]["is_default"] is False
    assert payload["lanes"]["crypto"]["is_default"] is False

    # Verify the Mongo doc actually has the merged field values.
    ln = await db[_TR_COLL].find_one({"_id": _LN_DOC})
    assert ln is not None
    assert ln["equity"] is True
    assert ln["crypto"] is False


@pytest.mark.asyncio
async def test_arm_with_partial_lanes_leaves_other_lanes_unchanged():
    """Specifying only one lane in the `lanes` map must NOT wipe the
    other lane's stored state — merge semantics only."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        # Pre-seed lanes: equity=True, crypto=False.
        await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "enabled": True,
                "reason": "test-unified-arm-06-seed",
                "lanes": {"equity": True, "crypto": False},
            },
        )
        # Now update ONLY crypto → True. Equity must stay True.
        r = await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "enabled": True,
                "reason": "test-unified-arm-06-flip",
                "lanes": {"crypto": True},
            },
        )
    payload = r.json()
    assert payload["lanes"]["equity"]["enabled"] is True, (
        "partial-lanes update wiped a lane it didn't reference — "
        "merge semantics violated"
    )
    assert payload["lanes"]["crypto"]["enabled"] is True


@pytest.mark.asyncio
async def test_arm_rejects_unknown_lane():
    """Unknown lane keys must 400 — no silent no-op that fools the
    operator into thinking their toggle stuck."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        r = await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "enabled": True,
                "reason": "test-unified-arm-07",
                "lanes": {"forex": True},
            },
        )
    assert r.status_code == 400
    assert "unknown lane" in r.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_lane_toggle_single_lane_without_touching_master():
    """POST /admin/trading/lane flips a single lane's enable state
    while leaving the master arm docs untouched. Fine-grained
    control for maintenance windows."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        # Arm master first.
        await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={"enabled": True, "reason": "test-unified-arm-08-arm"},
        )
        # Disarm crypto lane only.
        r = await client.post(
            "/api/admin/trading/lane",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "lane": "crypto",
                "enabled": False,
                "reason": "test-unified-arm-08-crypto-off",
            },
        )
    payload = r.json()
    # Master arm unchanged.
    assert payload["mc_switch"]["enabled"] is True
    assert payload["trader_switch"]["enabled"] is True
    # Crypto off, equity still default-True.
    assert payload["lanes"]["crypto"]["enabled"] is False
    assert payload["lanes"]["equity"]["enabled"] is True


@pytest.mark.asyncio
async def test_lane_toggle_requires_reason_when_enabling():
    """Enabling a lane requires a reason (audit trail). Disabling
    a lane does not — same as master arm doctrine."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        # Disable is fine with no reason.
        r_off = await client.post(
            "/api/admin/trading/lane",
            headers={"Authorization": f"Bearer {tok}"},
            json={"lane": "equity", "enabled": False, "reason": ""},
        )
        assert r_off.status_code == 200
        # Enable REQUIRES a reason.
        r_on = await client.post(
            "/api/admin/trading/lane",
            headers={"Authorization": f"Bearer {tok}"},
            json={"lane": "equity", "enabled": True, "reason": "   "},
        )
        assert r_on.status_code == 400


@pytest.mark.asyncio
async def test_arm_audit_row_captures_pre_and_post_lane_state():
    """The unified audit row must include pre/post state of BOTH
    master switches AND every known lane so a post-mortem can pin
    which flip touched what."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        tok = await _login(client)
        await client.post(
            "/api/admin/trading/arm",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "enabled": True,
                "reason": "test-unified-arm-09",
                "lanes": {"equity": True, "crypto": False},
            },
        )
    row = await db[_MC_AUDIT].find_one(
        {"reason": "test-unified-arm-09", "source": "unified_arm"},
    )
    assert row is not None
    assert row["pre_state"]["mc_switch"] is False
    assert row["pre_state"]["trader_switch"] is False
    assert row["pre_state"]["lanes"] == {"equity": True, "crypto": True}
    assert row["post_state"]["mc_switch"] is True
    assert row["post_state"]["trader_switch"] is True
    assert row["post_state"]["lanes"] == {"equity": True, "crypto": False}
    assert row["lanes_updated"] == ["equity", "crypto"] or (
        set(row["lanes_updated"]) == {"equity", "crypto"}
    )
