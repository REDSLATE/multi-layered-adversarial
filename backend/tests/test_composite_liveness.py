"""Composite per-loop liveness contract (2026-02-20).

Doctrine:
  MC's old badge collapsed every signal into one heartbeat-driven
  status. That hid the real failure mode the operator caught on
  2026-06-03: REDEYE marked DEAD by heartbeat but actively passing
  gate checks 45s ago. The brain was alive on its engine loop while
  its heartbeat loop had stalled.

  Composite liveness reports each loop independently and derives
  an overall band that respects "engine still firing" as proof of
  life even if heartbeat is stale.

What this pins:
  1. The `_composite_liveness` helper exists in the diagnose module.
  2. All six loops (heartbeat / checkin / engine / directional /
     sovereign / opinion) appear in the response.
  3. Six verdicts produce the right overall band:
       LIVE / LIVE_DEGRADED / LIVE_IDLE / STALE / DEAD / NEVER
  4. The REDEYE case (heartbeat dead, engine fresh) gets
     `LIVE_DEGRADED` and ENGINE_ACTIVE chip — NOT DEAD.
  5. The endpoint surfaces `composite_liveness` for every brain.
"""
from __future__ import annotations

import pytest


@pytest.mark.tripwire
def test_composite_liveness_helper_exists():
    with open("/app/backend/routes/brain_emission_diagnose.py") as f:
        src = f.read()
    assert "def _composite_liveness(" in src, (
        "_composite_liveness helper is missing — operator UI has no "
        "per-loop band to render"
    )
    # All six expected loops named in the helper.
    for loop in (
        "heartbeat_loop", "checkin_loop", "engine_loop",
        "directional_loop", "sovereign_loop", "opinion_loop",
    ):
        assert loop in src, f"composite liveness must surface {loop!r}"
    # All six overall verdicts named.
    for band in (
        "LIVE_DEGRADED", "LIVE_IDLE", "STALE", "DEAD", "NEVER",
    ):
        assert band in src, f"missing overall band: {band!r}"


# ─── unit tests on the band derivation ────────────────────────────────


def _hb(age=None, opinions=0, sov_age=None):
    return {
        "heartbeat_age_seconds": age,
        "contribution_age_seconds": sov_age,
        "opinions_last_hour": opinions,
    }


def _ck(age=None):
    return {"checkin_age_seconds": age}


def _em(engine_iso=None, directional_age=None):
    return {
        "latest_emission": {"ingest_ts": engine_iso} if engine_iso else None,
        "latest_directional_age_seconds": directional_age,
    }


def test_redeye_case_heartbeat_dead_engine_fresh_is_LIVE_DEGRADED():
    """The exact symptom the operator caught: heartbeat DEAD 308s,
    engine actively passing gate checks 45s ago. MUST produce
    LIVE_DEGRADED, NOT DEAD, with an ENGINE_ACTIVE chip."""
    from datetime import datetime, timedelta, timezone
    from routes.brain_emission_diagnose import _composite_liveness

    engine_iso = (
        datetime.now(timezone.utc) - timedelta(seconds=45)
    ).isoformat()
    out = _composite_liveness(
        heartbeat=_hb(age=308, sov_age=4 * 86_400),  # 4d sovereign silence
        checkin=_ck(age=180),
        emission=_em(engine_iso=engine_iso, directional_age=45),
    )
    assert out["overall"] == "LIVE_DEGRADED", (
        f"REDEYE-pattern must produce LIVE_DEGRADED; got {out['overall']!r}"
    )
    assert "ENGINE_ACTIVE" in out["chips"]
    assert "DEAD_HEARTBEAT" in out["chips"]
    assert "STALE_SOVEREIGN" in out["chips"]
    # Per-loop bands honest.
    assert out["loops"]["heartbeat_loop"]["band"] == "dead"
    assert out["loops"]["engine_loop"]["band"] == "live"
    assert out["loops"]["sovereign_loop"]["band"] == "dead"


def test_all_signals_fresh_is_LIVE():
    from datetime import datetime, timezone
    from routes.brain_emission_diagnose import _composite_liveness

    engine_iso = datetime.now(timezone.utc).isoformat()
    out = _composite_liveness(
        heartbeat=_hb(age=10, opinions=5, sov_age=30),
        checkin=_ck(age=20),
        emission=_em(engine_iso=engine_iso, directional_age=15),
    )
    assert out["overall"] == "LIVE"
    assert "ENGINE_ACTIVE" in out["chips"]


def test_heartbeat_live_but_no_engine_in_1h_is_LIVE_IDLE():
    from datetime import datetime, timedelta, timezone
    from routes.brain_emission_diagnose import _composite_liveness

    old_engine = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    out = _composite_liveness(
        heartbeat=_hb(age=10),
        checkin=_ck(age=15),
        emission=_em(engine_iso=old_engine, directional_age=7200),
    )
    assert out["overall"] == "LIVE_IDLE"


def test_heartbeat_stale_no_engine_is_STALE():
    from routes.brain_emission_diagnose import _composite_liveness

    out = _composite_liveness(
        heartbeat=_hb(age=200),
        checkin=_ck(age=200),
        emission=_em(engine_iso=None, directional_age=None),
    )
    assert out["overall"] == "STALE"
    assert "STALE_HEARTBEAT" in out["chips"]


def test_all_silent_is_DEAD():
    from routes.brain_emission_diagnose import _composite_liveness

    out = _composite_liveness(
        heartbeat=_hb(age=5000),
        checkin=_ck(age=5000),
        emission=_em(engine_iso=None, directional_age=None),
    )
    assert out["overall"] == "DEAD"
    assert "DEAD_HEARTBEAT" in out["chips"]


def test_never_contacted_is_NEVER():
    from routes.brain_emission_diagnose import _composite_liveness

    out = _composite_liveness(
        heartbeat=_hb(age=None),
        checkin=_ck(age=None),
        emission=_em(engine_iso=None, directional_age=None),
    )
    assert out["overall"] == "NEVER"


# ─── behavioral test on the live endpoint ─────────────────────────────


def test_endpoint_surfaces_composite_liveness(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose/alpha", timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "composite_liveness" in body, (
        "diagnose response must include composite_liveness — operator "
        "UI has no per-loop view otherwise"
    )
    cl = body["composite_liveness"]
    assert cl["overall"] in (
        "LIVE", "LIVE_DEGRADED", "LIVE_IDLE",
        "STALE", "DEAD", "NEVER",
    )
    for loop in (
        "heartbeat_loop", "checkin_loop", "engine_loop",
        "directional_loop", "sovereign_loop", "opinion_loop",
    ):
        assert loop in cl["loops"]
        assert "band" in cl["loops"][loop]
