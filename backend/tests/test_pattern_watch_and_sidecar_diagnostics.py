"""Tripwires for the Pattern Watch endpoint (`/api/admin/patterns/scan`)
and the Sidecar Diagnostics aggregator (`/api/admin/sidecar-diagnostics`).

Doctrine pins:
  * Pattern Watch is DESCRIPTIVE EVIDENCE ONLY. Never carries
    authority. Filters operate over `shared_pattern_snapshots`
    populated by the technical feed (pass #10).
  * Sidecar Diagnostics is READ-ONLY. Never modifies seats, never
    reroutes traffic. One curl returns every signal needed to
    triage "is this brain alive, contributing, emitting, discussing?"
"""
from __future__ import annotations

import os

import pytest
import requests


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}: {r.text}")
    return r.json()["access_token"]


# ────────────────────────── Pattern Watch ──────────────────────────


@pytest.mark.tripwire
def test_pattern_scan_requires_auth():
    r = requests.get(f"{BASE_URL}/api/admin/patterns/scan", timeout=15)
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_pattern_scan_canonical_response_shape():
    """Schema is locked — Overview tile depends on these keys."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/patterns/scan?limit=20&min_score=0.0",
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("filters", "count", "tier_counts", "items", "doctrine"):
        assert key in body, f"missing top-level key {key}"
    assert set(body["tier_counts"].keys()) == {
        "breakout_active", "consolidation_only", "uptrend_only",
    }
    # Filters echo back what was sent.
    assert body["filters"]["limit"] == 20
    assert body["filters"]["min_score"] == 0.0


@pytest.mark.tripwire
def test_pattern_scan_items_have_pinned_keys():
    """Each item row exposes the keys the Overview tile renders.
    Schema drift breaks the UI silently."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/patterns/scan?limit=5&min_score=0.0",
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    if not items:
        pytest.skip("no pattern snapshots populated yet — schema test skipped")
    expected = {
        "symbol", "tf", "source", "setup_score",
        "ma200_uptrend", "consolidation", "consolidation_duration_bars",
        "breakout", "breakout_pct", "volume_surge_multiple",
        "bars_since_breakout", "small_cap_qualified",
        "last_close", "last_bar_ts", "computed_at",
    }
    assert set(items[0].keys()) == expected, (
        f"item key set drifted: missing={expected - set(items[0])} "
        f"extra={set(items[0]) - expected}"
    )


@pytest.mark.tripwire
def test_pattern_scan_min_score_filter_applies():
    """Items returned must satisfy the min_score filter."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/patterns/scan?limit=20&min_score=0.8",
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    assert r.status_code == 200
    for it in r.json()["items"]:
        assert it["setup_score"] >= 0.8, (
            f"min_score filter leaked: returned {it['setup_score']}"
        )


@pytest.mark.tripwire
def test_pattern_scan_breakout_only_filter():
    """When breakout_only=true, every row must have breakout=True."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/patterns/scan?limit=20&min_score=0.0&breakout_only=true",
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    assert r.status_code == 200
    for it in r.json()["items"]:
        assert it["breakout"] is True


@pytest.mark.tripwire
def test_pattern_scan_doctrine_note_present():
    """The doctrine note must remind readers this is evidence, not authority."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/patterns/scan", timeout=20,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    doctrine = r.json().get("doctrine", "").lower()
    assert "evidence" in doctrine
    assert "never" in doctrine and ("authority" in doctrine or "trigger" in doctrine)


# ────────────────────────── Sidecar Diagnostics ──────────────────────────


@pytest.mark.tripwire
def test_sidecar_diagnostics_requires_auth():
    r = requests.get(f"{BASE_URL}/api/admin/sidecar-diagnostics", timeout=15)
    assert r.status_code in (401, 403)


@pytest.mark.tripwire
def test_sidecar_diagnostics_canonical_shape():
    """Top-level keys are locked: dashboard tile depends on them."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/sidecar-diagnostics",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("generated_at", "fleet", "brains", "doctrine_note"):
        assert key in body, f"missing top-level key {key}"


@pytest.mark.tripwire
def test_sidecar_diagnostics_fleet_summary_keys():
    """Fleet roll-up must expose every verdict tier + role counters."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/sidecar-diagnostics",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200
    fleet = r.json()["fleet"]
    expected = {
        "total_brains", "connected", "partial", "stale", "dead", "never",
        "brains_with_no_intents_ever", "brains_with_no_opinions_ever",
    }
    assert set(fleet.keys()) == expected


@pytest.mark.tripwire
def test_sidecar_diagnostics_per_brain_shape():
    """Each brain row must expose all five signal channels:
    heartbeat, sovereign_contribution, intents, opinions, verdict.
    The 21k mystery hinged on misreading sovereign_audit_log totals
    as intent backlogs — this schema makes each counter explicit so
    no future reader can confuse them again."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/sidecar-diagnostics",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200
    brains = r.json()["brains"]
    assert len(brains) >= 4, "must report at least the 4 canonical brains"
    for b in brains:
        for key in (
            "brain", "verdict", "operator_hint",
            "heartbeat", "sovereign_contribution", "intents", "opinions",
        ):
            assert key in b, f"brain {b.get('brain')!r} missing key {key}"
        # Sub-channel shapes
        assert set(b["heartbeat"].keys()) == {
            "last_seen", "age_seconds", "count", "fresh",
        }
        assert set(b["sovereign_contribution"].keys()) == {
            "last_seen", "age_seconds", "live_count",
            "audit_log_total", "fresh",
        }
        assert set(b["intents"].keys()) == {
            "total", "last_seen", "age_seconds",
            "latest_symbol", "latest_action", "latest_lane", "latest_gate_state",
        }
        assert set(b["opinions"].keys()) == {
            "total", "last_seen", "age_seconds",
        }


@pytest.mark.tripwire
def test_sidecar_diagnostics_verdict_vocabulary_pinned():
    """Verdict vocabulary must match LivePulse classifier so the
    dashboard panels never disagree. Adding new verdicts here without
    updating the LivePulse component is a silent contract break."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/sidecar-diagnostics",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200
    valid = {"connected", "partial", "stale", "dead", "never"}
    for b in r.json()["brains"]:
        assert b["verdict"] in valid, (
            f"unknown verdict {b['verdict']!r} for brain {b['brain']!r}"
        )


@pytest.mark.tripwire
def test_sidecar_diagnostics_doctrine_note_explains_audit_log():
    """The doctrine note must explain that sovereign_audit_log totals
    are HEALTHY heartbeats, not backlogs. This is the lesson from the
    21k misread — pinned here so it never gets forgotten."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/sidecar-diagnostics",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    assert r.status_code == 200
    note = r.json()["doctrine_note"].lower()
    assert "audit" in note
    assert "heartbeat" in note or "healthy" in note
    assert "backlog" in note or "not a" in note
