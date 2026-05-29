"""RedEye decision-log diagnostics wiring — tripwire (2026-05-29).

After RedEye's iter-106z13 work it now has its own canonical
`redeye_decision_log` collection (parity with alpha/camaro/chevelle).
MC's diagnostics column MUST read from it so the operator sees a true
intent count for RedEye instead of the opinion-post fallback the
column was using before.

Contract owner: RedEye team. See
/app/memory/MC_HANDOFF_redeye_decision_log.md
"""
from __future__ import annotations

import inspect

import pytest

from shared import diagnostics as diag_mod


pytestmark = [pytest.mark.tripwire]


def test_namespaces_export_redeye_decision_log():
    """The canonical collection name MUST live in namespaces."""
    import namespaces
    assert hasattr(namespaces, "REDEYE_DECISION_LOG"), (
        "REDEYE_DECISION_LOG export removed from namespaces.py — "
        "MC's diagnostics column will silently fall back to the "
        "opinion-count proxy and the operator will see the wrong "
        "number on the Health & Liveness table."
    )
    assert namespaces.REDEYE_DECISION_LOG == "redeye_decision_log", (
        "The collection name MUST match the RedEye team's contract "
        "(see MC_HANDOFF_redeye_decision_log.md). Renaming it here "
        "without coordinating breaks the dashboard."
    )


def test_runtime_log_count_routes_redeye_to_decision_log():
    """`_runtime_log_count('redeye')` MUST read from
    `redeye_decision_log`, NOT fall back to SHARED_OPINIONS."""
    src = inspect.getsource(diag_mod._runtime_log_count)
    assert "REDEYE_DECISION_LOG" in src or '"redeye"' in src, (
        "diagnostics._runtime_log_count no longer references REDEYE "
        "explicitly — RedEye will fall back to opinion-count and the "
        "dashboard will show the wrong metric."
    )
    # Belt-and-braces: make sure redeye is in the routing dict literal.
    assert '"redeye"' in src, (
        "redeye missing from the per-brain collection map"
    )


@pytest.mark.asyncio
async def test_runtime_log_count_redeye_reads_redeye_collection(monkeypatch):
    """Behavioural: call the helper with stack='redeye' and confirm it
    reads from `redeye_decision_log` — NOT from `shared_brain_opinions`."""
    from db import db
    import uuid

    marker = f"_tripwire-{uuid.uuid4()}"
    # Insert a tagged row in redeye_decision_log
    await db["redeye_decision_log"].insert_one({
        "decision_id": marker,
        "_test_marker": True,
    })
    try:
        count = await diag_mod._runtime_log_count("redeye")
        assert count >= 1, (
            "_runtime_log_count('redeye') returned 0 with a row "
            "present in redeye_decision_log — wiring is wrong, MC "
            "is still reading the opinion fallback."
        )
    finally:
        await db["redeye_decision_log"].delete_one({"decision_id": marker})


def test_routing_table_includes_all_four_brains():
    """The brain → collection map MUST cover all four LIVE_RUNTIMES so
    every brain row on the dashboard reports a real decision-log count."""
    from namespaces import LIVE_RUNTIMES
    src = inspect.getsource(diag_mod._runtime_log_count)
    for brain in LIVE_RUNTIMES:
        assert f'"{brain}"' in src, (
            f"brain {brain!r} missing from _runtime_log_count routing "
            f"— will fall back to opinion count silently"
        )
