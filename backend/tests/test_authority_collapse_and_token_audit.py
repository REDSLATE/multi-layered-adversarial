"""Tripwires — authority-ladder collapse + runtime-token audit (2026-05-26).

Doctrine pin:
    `LIVE EXEC` is computed from SEAT POLICY only. The `authority_state`
    field remains as informational metadata. Brain in executor/crypto
    seat with `may_execute=True` policy = execution_allowed. Anything else
    = not allowed.
"""
from __future__ import annotations

import pytest
import requests

from db import db


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


# ─── A — authority no longer gates ───


async def test_overview_exposes_seat_and_authority_separately():
    tok = _login()
    r = requests.get(f"{BASE_URL}/api/shared/overview", headers=_hdr(tok), timeout=10)
    assert r.status_code == 200
    runtimes = r.json().get("runtimes", [])
    assert len(runtimes) > 0
    for row in runtimes:
        assert "authority_state" in row, "authority_state must persist as informational"
        assert "execution_allowed" in row
        assert "current_seat" in row, "seat must be exposed for transparency"


async def test_executor_seat_grants_execution():
    tok = _login()
    r = requests.get(f"{BASE_URL}/api/shared/overview", headers=_hdr(tok), timeout=10)
    runtimes = r.json().get("runtimes", [])
    by_brain = {r["runtime"]: r for r in runtimes}
    for row in runtimes:
        if row.get("current_seat") == "executor":
            assert row["execution_allowed"] is True, (
                f"{row['runtime']} in executor seat must have execution_allowed"
            )
        if row.get("current_seat") == "governor":
            assert row["execution_allowed"] is False, (
                f"governor seat may NEVER execute"
            )


async def test_seatless_brain_cannot_execute():
    tok = _login()
    r = requests.get(f"{BASE_URL}/api/shared/overview", headers=_hdr(tok), timeout=10)
    runtimes = r.json().get("runtimes", [])
    for row in runtimes:
        if row.get("current_seat") is None:
            assert row["execution_allowed"] is False


async def test_authority_state_does_not_force_execution():
    """A brain with authority_state=co_trader but NO executor seat
    must still show execution_allowed=False. This is the doctrine
    inversion — seat is the gate, not state."""
    tok = _login()
    r = requests.get(f"{BASE_URL}/api/shared/overview", headers=_hdr(tok), timeout=10)
    runtimes = r.json().get("runtimes", [])
    for row in runtimes:
        if (
            row.get("authority_state") in ("co_trader", "primary")
            and row.get("current_seat") not in ("executor", "crypto")
        ):
            assert row["execution_allowed"] is False


# ─── B — runtime-token rejection audit ───


async def test_wrong_token_logs_rejection():
    """A POST with a wrong runtime token must:
      1. Return 401
      2. Surface as a rejection row in /admin/runtime-tokens/health
    """
    # Clear baseline
    await db["runtime_token_rejections"].delete_many({"runtime": "alpha"})
    r = requests.post(
        f"{BASE_URL}/api/intents",
        json={"stack": "alpha", "action": "HOLD", "symbol": "AAPL",
              "lane": "equity", "confidence": 0.5, "rationale": "audit"},
        headers={"X-Runtime-Token": "DEFINITELY_WRONG_VALUE",
                 "Content-Type": "application/json"},
        timeout=10,
    )
    assert r.status_code == 401
    # Allow the async fire-and-forget write to land.
    import asyncio as _a
    await _a.sleep(0.5)
    tok = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime-tokens/health",
        headers=_hdr(tok), timeout=10,
    )
    assert r.status_code == 200
    by_brain = {row["brain"]: row for row in r.json()["rows"]}
    assert by_brain["alpha"]["rejections_total"] >= 1
    assert "token_mismatch" in by_brain["alpha"]["rejections_by_reason"]
    await db["runtime_token_rejections"].delete_many({"runtime": "alpha"})


async def test_token_health_endpoint_lists_all_brains():
    tok = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime-tokens/health",
        headers=_hdr(tok), timeout=10,
    )
    assert r.status_code == 200
    brains = {row["brain"] for row in r.json()["rows"]}
    assert brains == {"alpha", "camaro", "chevelle", "redeye"}


async def test_diagnosis_field_present():
    tok = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime-tokens/health",
        headers=_hdr(tok), timeout=10,
    )
    for row in r.json()["rows"]:
        assert row["diagnosis"] in {
            "healthy", "minor_rejections", "token_mismatch_high_volume",
            "token_not_configured_on_mc", "header_missing_high_volume",
        }
