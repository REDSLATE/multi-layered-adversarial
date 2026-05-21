"""HTTP tests for `/api/admin/brain/emission-diagnose`.

Doctrine pin (2026-02-18):
    The diagnostic is READ-ONLY. It MUST never mutate state and MUST
    surface a typed `silent_reasons` list so the operator can act
    without parsing free-form messages.
"""
from __future__ import annotations

import pytest
import requests


@pytest.mark.tripwire
def test_emission_diagnose_requires_auth(base_url):
    r = requests.get(
        f"{base_url}/api/admin/brain/emission-diagnose", timeout=15,
    )
    # FastAPI returns 401/403 depending on the auth dep config.
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_emission_diagnose_returns_all_four_brains(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose",
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "rows" in body
    brains = sorted(row["brain"] for row in body["rows"])
    assert brains == ["alpha", "camaro", "chevelle", "redeye"]


@pytest.mark.tripwire
def test_emission_diagnose_row_shape(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose",
        timeout=30,
    )
    body = r.json()
    for row in body["rows"]:
        assert {"brain", "summary", "silent_reasons", "heartbeat",
                "sidecar_checkin", "roster", "emission"} <= set(row)
        # silent_reasons must be a list of typed strings.
        assert isinstance(row["silent_reasons"], list)
        for reason in row["silent_reasons"]:
            assert isinstance(reason, str)
            assert reason == reason.upper(), (
                f"reason {reason!r} should be UPPERCASE typed code"
            )
        # emission counts must be present.
        em = row["emission"]
        for k in ("by_action", "by_gate_state", "by_lane",
                  "total_intents_ever", "window_total"):
            assert k in em, f"missing emission.{k} on brain={row['brain']}"


@pytest.mark.tripwire
def test_emission_diagnose_single_brain(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose/alpha",
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brain"] == "alpha"
    assert "silent_reasons" in body


@pytest.mark.tripwire
def test_emission_diagnose_unknown_brain_404(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose/nonsense",
        timeout=30,
    )
    assert r.status_code == 404


@pytest.mark.tripwire
def test_emission_diagnose_classifies_silent_reasons_unit():
    """Unit test for the typed-reason classifier — no HTTP, no DB."""
    from routes.brain_emission_diagnose import _classify_silent_reasons

    # A brain that never contacted MC at all.
    reasons = _classify_silent_reasons(
        brain="alpha",
        heartbeat={"ever_heartbeated": False, "heartbeat_band": "never"},
        checkin={"ever_checked_in": False, "verdict": "never"},
        roster={"holds_equity_executor": True, "holds_crypto_executor": False},
        emission={
            "total_intents_ever": 0, "window_total": 0,
            "by_action": {"BUY": 0, "SELL": 0, "SHORT": 0, "COVER": 0, "HOLD": 0},
            "audit_only_rejections_in_window": 0,
            "latest_directional_emission": None,
            "latest_directional_age_seconds": None,
        },
    )
    assert "NO_HEARTBEAT_EVER" in reasons
    assert "NO_SIDECAR_CHECKIN" in reasons
    assert "NO_INTENT_EVER" in reasons
    assert "PRODUCING_ROUTABLE_INTENTS" not in reasons


@pytest.mark.tripwire
def test_emission_diagnose_classifies_hold_only_brain():
    """A brain that's healthy on liveness but emits ONLY HOLDs (the
    exact prod-screenshot Camaro symptom)."""
    from routes.brain_emission_diagnose import _classify_silent_reasons

    reasons = _classify_silent_reasons(
        brain="camaro",
        heartbeat={"ever_heartbeated": True, "heartbeat_band": "fresh"},
        checkin={"ever_checked_in": True, "verdict": "prod"},
        roster={"holds_equity_executor": False, "holds_crypto_executor": False},
        emission={
            "total_intents_ever": 500, "window_total": 100,
            "by_action": {"BUY": 0, "SELL": 0, "SHORT": 0, "COVER": 0, "HOLD": 100},
            "audit_only_rejections_in_window": 0,
            "latest_directional_emission": None,
            "latest_directional_age_seconds": None,
        },
    )
    assert "ONLY_HOLD_ACTIONS" in reasons
    assert "NO_EXECUTOR_SEAT_FOR_LANE" in reasons
    assert "PRODUCING_ROUTABLE_INTENTS" not in reasons


@pytest.mark.tripwire
def test_emission_diagnose_happy_path_classification():
    from routes.brain_emission_diagnose import _classify_silent_reasons

    reasons = _classify_silent_reasons(
        brain="alpha",
        heartbeat={"ever_heartbeated": True, "heartbeat_band": "fresh"},
        checkin={"ever_checked_in": True, "verdict": "prod"},
        roster={"holds_equity_executor": True, "holds_crypto_executor": False},
        emission={
            "total_intents_ever": 50, "window_total": 12,
            "by_action": {"BUY": 5, "SELL": 3, "SHORT": 0, "COVER": 0, "HOLD": 4},
            "audit_only_rejections_in_window": 0,
            "latest_directional_emission": {"action": "BUY"},
            "latest_directional_age_seconds": 600,
        },
    )
    assert "PRODUCING_ROUTABLE_INTENTS" in reasons
