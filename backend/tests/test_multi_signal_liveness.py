"""Tripwires — multi-signal liveness (2026-05-26).

Doctrine pin:
    A brain is `active` if ANY of: heartbeat<2m, sovereign<5m, intent
    last hour, opinion last hour. `dormant` if reachable but quiet.
    `dead` only when no signal at all. Prevents false-DEAD on a brain
    that's intent-busy but sovereign-silent (Camaro's case).
"""
from __future__ import annotations

import pytest
import requests


BASE_URL = "http://localhost:8001"
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


pytestmark = pytest.mark.asyncio


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    return r.json()["access_token"]


def _hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _rows():
    tok = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/brain/emission-diagnose",
        headers=_hdr(tok), timeout=10,
    )
    assert r.status_code == 200
    return r.json().get("rows", [])


async def test_liveness_field_present_for_all_brains():
    for row in _rows():
        hb = row.get("heartbeat", {})
        assert hb.get("liveness") in {"active", "dormant", "dead"}, (
            f"{row['brain']} missing valid liveness"
        )


async def test_multi_signal_indicators_present():
    for row in _rows():
        hb = row.get("heartbeat", {})
        signals = hb.get("signals") or {}
        assert "heartbeat_fresh" in signals
        assert "sovereign_fresh" in signals
        assert "intent_recent" in signals
        assert "opinion_recent" in signals


async def test_intent_counts_exposed():
    for row in _rows():
        hb = row.get("heartbeat", {})
        assert "intents_last_hour" in hb
        assert "intents_last_24h" in hb
        assert "opinions_last_hour" in hb
        assert "opinions_last_24h" in hb


async def test_any_recent_intent_implies_not_dead():
    """A brain with intents in the last hour MUST NOT be classified dead."""
    for row in _rows():
        hb = row.get("heartbeat", {})
        if hb.get("intents_last_hour", 0) > 0:
            assert hb["liveness"] != "dead", (
                f"{row['brain']} has fresh intents but is classified dead"
            )


async def test_sovereign_silent_intent_busy_is_active_not_dead():
    """Specifically: a brain that's sov-silent but intent-busy is ACTIVE."""
    for row in _rows():
        hb = row.get("heartbeat", {})
        if (
            hb.get("intents_last_hour", 0) > 0
            and (hb.get("contribution_age_seconds") or 999999) > 600
        ):
            assert hb["liveness"] == "active", (
                f"{row['brain']} should be active despite sovereign silence"
            )
