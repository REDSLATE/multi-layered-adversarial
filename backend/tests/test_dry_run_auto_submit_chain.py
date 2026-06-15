"""Regression test for the 2026-02-19 prod incident.

Operator reported "still not trading" on Monday at market open with
Shelly's policy ENABLED. Production funnel showed:
  shelly_eligible (2967) → submitted (0) — 100% drop.

Investigation:
  `auto_dry_run_drain` and `POST /execution/run-dry-run` both called
  `run_dry_run_for_intent` DIRECTLY without chaining `maybe_auto_submit`.
  Eligible intents transitioned to `dry_run_passed` and stopped —
  Shelly was never invoked, no audit row written, all 2965 leaked
  into "Never submitted (no audit row)".

This test pins both call sites: a would_pass dry-run MUST invoke
maybe_auto_submit so Shelly gets a chance.
"""
from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


async def test_manual_dry_run_endpoint_chains_to_auto_submit():
    """POST /execution/dry_run must call maybe_auto_submit after
    a would_pass result so Shelly gets a chance."""
    from shared import execution as ex
    from shared.execution import execution_dry_run

    fake_user = {"email": "test@x"}
    fake_result = {"verdict": "would_pass", "gates": [], "would_have_blocked": False}

    with patch.object(ex, "run_dry_run_for_intent", new_callable=AsyncMock,
                      return_value=fake_result) as mock_dr, \
         patch("shared.auto_submit_policy.maybe_auto_submit",
               new_callable=AsyncMock, return_value=None) as mock_as:
        r = await execution_dry_run(
            intent_id="test-intent", order_notional_usd=5.0, user=fake_user,
        )

    assert mock_dr.called, "dry-run must be invoked"
    assert mock_as.called, (
        "maybe_auto_submit MUST be invoked after a would_pass result — "
        "this is the 2026-02-19 leak fix"
    )
    assert mock_as.call_args.args[0] == "test-intent"
    assert r["verdict"] == "would_pass"


async def test_manual_dry_run_skips_auto_submit_when_blocked():
    """If dry-run says would_block, maybe_auto_submit should NOT be
    called — no point submitting an intent that already failed gates."""
    from shared import execution as ex
    from shared.execution import execution_dry_run

    fake_user = {"email": "test@x"}
    fake_result = {"verdict": "would_block", "gates": [], "would_have_blocked": True}

    with patch.object(ex, "run_dry_run_for_intent", new_callable=AsyncMock,
                      return_value=fake_result), \
         patch("shared.auto_submit_policy.maybe_auto_submit",
               new_callable=AsyncMock) as mock_as:
        r = await execution_dry_run(
            intent_id="bad-intent", order_notional_usd=5.0, user=fake_user,
        )

    assert not mock_as.called, "maybe_auto_submit must NOT be called for blocked intents"
    assert r["verdict"] == "would_block"


async def test_auto_dry_run_drain_chains_to_auto_submit():
    """The catch-up drain (auto_dry_run_drain) was the bigger leak —
    backlog intents passed dry-run then sat there. Must chain too."""
    from db import db
    from namespaces import SHARED_INTENTS
    from shared import execution as ex
    from shared.execution import auto_dry_run_drain

    # Seed 3 pending intents in the test db.
    test_ids = ["DRAIN_TEST_1", "DRAIN_TEST_2", "DRAIN_TEST_3"]
    try:
        for iid in test_ids:
            await db[SHARED_INTENTS].insert_one({
                "intent_id": iid, "gate_state": "pending",
                "stack": "alpha", "symbol": "DRAIN_TEST",
                "ingest_ts": "2026-02-19T10:00:00Z",
            })
        with patch.object(ex, "run_dry_run_for_intent", new_callable=AsyncMock,
                          return_value={"verdict": "would_pass"}), \
             patch("shared.auto_submit_policy.maybe_auto_submit",
                   new_callable=AsyncMock, return_value={"ok": True}) as mock_as:
            r = await auto_dry_run_drain(
                limit=10, stack="alpha", user={"email": "test@x"},
            )
        # Filter call args to only those for our 3 seeded test intents
        # (preview db may have other DRAIN-targeted backlog).
        called_ids = [c.args[0] for c in mock_as.call_args_list]
        for iid in test_ids:
            assert iid in called_ids, f"drain must chain to auto_submit for {iid}"
        assert r["auto_submitted"] >= 3
    finally:
        await db[SHARED_INTENTS].delete_many({"intent_id": {"$in": test_ids}})


async def test_drain_records_auto_submit_failures():
    """If auto_submit raises, the drain must record it in failures
    list so post-mortem surfaces the cause."""
    from db import db
    from namespaces import SHARED_INTENTS
    from shared import execution as ex
    from shared.execution import auto_dry_run_drain

    iid = "DRAIN_FAIL_TEST"
    try:
        await db[SHARED_INTENTS].insert_one({
            "intent_id": iid, "gate_state": "pending",
            "stack": "alpha", "symbol": "DRAIN_FAIL",
            "ingest_ts": "2026-02-19T10:00:00Z",
        })
        with patch.object(ex, "run_dry_run_for_intent", new_callable=AsyncMock,
                          return_value={"verdict": "would_pass"}), \
             patch("shared.auto_submit_policy.maybe_auto_submit",
                   new_callable=AsyncMock, side_effect=RuntimeError("broker down")):
            r = await auto_dry_run_drain(
                limit=1, stack="alpha", user={"email": "test@x"},
            )
        # find our specific failure
        matching = [f for f in r["failures"] if f.get("intent_id") == iid]
        assert matching, "drain failure for this intent must be recorded"
        assert "auto_submit" in matching[0]["error"]
    finally:
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})
