"""Tripwires: drift detector decoupling + promotion artifact governor exclusion.

Doctrine pins (2026-02-18):

  HEARTBEAT TIER — liveness band only. Must not infer URL config from
  age. The tiers are { ok, stale, dead, unknown }. The legacy
  `preview_drift` tier (which conflated "stale" with "wrong MC URL")
  was removed because it produced false alarms on every slow LLM
  cycle. The actual MC-URL verdict lives in
  `sidecar_checkin._verdict_from_validation`, which inspects the
  brain's stamped env_name + mc_url.

  PROMOTION ARTIFACT — never evaluates a brain currently holding
  GOVERNOR authority. Governor is off-ladder (mirrors
  `promote_brain`'s 400-refusal). Chevelle holds both equity and
  crypto governor seats; including it in shadow-vs-fill comparisons
  is a category error.
"""
from __future__ import annotations

import pytest

from shared.diagnostics import _heartbeat_tier


# ─── Heartbeat tier — liveness-only, no URL inference ───────────────


@pytest.mark.tripwire
def test_heartbeat_tier_returns_only_liveness_bands():
    """The function MUST return ONLY {ok, stale, dead, unknown}. The
    legacy `preview_drift` / `drift` tiers must not return — they
    leaked URL inference into a pure-age check.

    Bands (2026-02-19 tuning — see namespaces.py):
      ok    < HEARTBEAT_OK_BELOW_SECONDS (120s)
      stale < HEARTBEAT_PREVIEW_DRIFT_SECONDS (300s)
      dead  ≥ HEARTBEAT_PREVIEW_DRIFT_SECONDS (300s)
    """
    assert _heartbeat_tier(None) == "unknown"
    assert _heartbeat_tier(0) == "ok"
    assert _heartbeat_tier(10) == "ok"
    # Just under the stale threshold (HEARTBEAT_OK_BELOW_SECONDS is 120).
    assert _heartbeat_tier(119) == "ok"
    # Past `ok` but under the dead threshold (300).
    assert _heartbeat_tier(120) == "stale"
    assert _heartbeat_tier(299) == "stale"
    # Past the dead threshold.
    assert _heartbeat_tier(300) == "dead"
    assert _heartbeat_tier(660) == "dead"
    assert _heartbeat_tier(99_999) == "dead"


@pytest.mark.tripwire
def test_heartbeat_tier_never_returns_preview_drift():
    """The string `preview_drift` is the literal that triggered the
    false 'on preview URL' banner. It must NEVER come back from this
    function under any input."""
    for age in (None, 0, 1, 30, 60, 109, 110, 300, 600, 1800, 36_000):
        result = _heartbeat_tier(age)
        assert result != "preview_drift", (
            f"age={age!r} returned the forbidden tier 'preview_drift'; "
            f"this conflates stale heartbeats with wrong-URL config"
        )
        assert result != "drift", (
            f"age={age!r} returned legacy tier 'drift'; "
            f"the canonical bands are now ok/stale/dead/unknown"
        )


# ─── Diagnostics integration: surfaces new tiers via HTTP ────────────


@pytest.mark.tripwire
def test_diagnostics_runtime_rows_carry_new_tier(auth_client, base_url):
    """Every runtime row must include a `heartbeat_tier` field whose
    value is one of the four canonical bands."""
    r = auth_client.get(f"{base_url}/api/admin/diagnostics", timeout=20)
    body = r.json()
    assert "runtimes" in body
    for row in body["runtimes"]:
        assert "heartbeat_tier" in row, f"missing heartbeat_tier on {row.get('runtime')}"
        assert row["heartbeat_tier"] in {"ok", "stale", "dead", "unknown"}, (
            f"{row.get('runtime')} carries a non-canonical heartbeat_tier "
            f"{row['heartbeat_tier']!r} — only ok/stale/dead/unknown are allowed"
        )


# ─── Promotion artifact — excludes governors ─────────────────────────


@pytest.mark.tripwire
async def test_promotion_artifact_excludes_governors_unit():
    """Direct unit test of the report function. Set chevelle's
    authority_state to 'governor' (its default), then call the all-
    brains endpoint and assert chevelle is excluded."""
    from db import db
    from namespaces import SHARED_AUTHORITY_STATE

    # Ensure chevelle is governor (its default authority).
    await db[SHARED_AUTHORITY_STATE].update_one(
        {"runtime": "chevelle"},
        {"$set": {"runtime": "chevelle", "authority_state": "governor"}},
        upsert=True,
    )
    # camaro is the default trading-shadow brain — should remain on-ladder.
    await db[SHARED_AUTHORITY_STATE].update_one(
        {"runtime": "camaro"},
        {"$set": {"runtime": "camaro", "authority_state": "observer"}},
        upsert=True,
    )

    from shared.promotion_artifact_report import get_promotion_artifact_all
    # Call the FastAPI route function directly so we don't need an HTTP
    # client + auth shim. `_user` is unused inside the body.
    result = await get_promotion_artifact_all(
        hours=24, benchmark_brain="alpha", _user={"email": "test@test.com"},
    )
    report_brains = [r["brain"] for r in result["reports"]]
    assert "chevelle" not in report_brains, (
        "Chevelle (governor) must not appear in promotion-artifact reports; "
        f"got {report_brains!r}"
    )
    assert "chevelle" in result.get("excluded_governors", []), (
        "excluded_governors list must surface the skipped governor for UI"
    )
    assert "alpha" not in report_brains  # benchmark always excluded
    # camaro stays (non-governor, non-benchmark).
    assert "camaro" in report_brains


@pytest.mark.tripwire
def test_promotion_artifact_response_shape_includes_excluded(auth_client, base_url):
    """Locked contract: the response must carry an `excluded_governors`
    array so the operator UI can surface "off-ladder" brains without
    inferring them from a missing row."""
    r = auth_client.get(
        f"{base_url}/api/admin/promotion-artifact?hours=24&benchmark_brain=alpha",
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "excluded_governors" in body
    assert isinstance(body["excluded_governors"], list)
