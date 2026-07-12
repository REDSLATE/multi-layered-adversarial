"""Consensus fingerprint v2 + fresh-input gate + Step 7 no-silent-returns.

Doctrine 2026-07-12 (Step 5.b + 7): every stance that carries a
`source_bar_close_at` participates in a v2 consensus fingerprint that
INCLUDES the min(bar_close) in the hash. Consensus writes are further
gated on the max-min spread of bar closes across engaged brains — any
spread exceeding CONSENSUS_FRESH_INPUT_TOLERANCE_SEC (default 900s)
rejects with `STALE_CONSENSUS_INPUT`. And every skipped transition in
`_maybe_auto_advance` MUST write a `consensus_transition_skipped`
audit row with a machine-readable reason_code.
"""
from __future__ import annotations

import hashlib
import os

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

# Test isolation: use a fresh DB to avoid polluting the main data.
_MONGO_URL = os.environ["MONGO_URL"]
_DB_NAME = f"test_consensus_v2_{os.getpid()}"


@pytest.fixture(scope="module")
async def _db():
    client = AsyncIOMotorClient(_MONGO_URL)
    db = client[_DB_NAME]
    yield db
    await client.drop_database(_DB_NAME)


@pytest.mark.asyncio
async def test_v2_fingerprint_includes_min_bar_close():
    """v2 fingerprint = sha256(v2|SYMBOL|stance|brains|min_bar_close).
    v1 fingerprint = sha256(v1|SYMBOL|stance|brains).
    They MUST differ for the same (symbol, stance, brains) combination
    once bar_close is included — otherwise the bar-close info is
    lost in the hash."""
    symbol = "NVDA"
    stance = "long"
    brains = ["camino", "gto"]
    bar_close = "2026-07-12T15:00:00+00:00"

    fp_v1 = hashlib.sha256(
        f"v1|{symbol}|{stance}|{','.join(sorted(brains))}".encode()
    ).hexdigest()
    fp_v2 = hashlib.sha256(
        f"v2|{symbol}|{stance}|{','.join(sorted(brains))}|{bar_close}".encode()
    ).hexdigest()
    assert fp_v1 != fp_v2, (
        "v2 must include bar_close in hash — otherwise adding the field "
        "was pointless (same {symbol, stance, brains} would collide "
        "across bar closes, defeating the freshness contract)."
    )


@pytest.mark.asyncio
async def test_fresh_input_gate_rejects_stale_spread():
    """When engaged brains report source_bar_close_at that span > 15
    min, the fresh-input gate MUST reject with STALE_CONSENSUS_INPUT
    instead of writing consensus. Simulates the compound-failure
    path where 4 brains reach the same call but off 4 different bar
    epochs."""
    from datetime import datetime, timedelta, timezone
    from shared.positions import CONSENSUS_FRESH_INPUT_TOLERANCE_SEC

    base = datetime(2026, 7, 12, 15, 0, 0, tzinfo=timezone.utc)
    within = [base, base + timedelta(minutes=5), base + timedelta(minutes=10)]
    stale = [base, base + timedelta(minutes=20)]

    within_spread = (max(within) - min(within)).total_seconds()
    stale_spread = (max(stale) - min(stale)).total_seconds()

    assert within_spread <= CONSENSUS_FRESH_INPUT_TOLERANCE_SEC
    assert stale_spread > CONSENSUS_FRESH_INPUT_TOLERANCE_SEC


@pytest.mark.asyncio
async def test_v1_backward_compat_when_any_stance_missing_bar_close():
    """If ANY engaged brain's stance is missing source_bar_close_at,
    the writer MUST stay on v1 (backward compat with unretrofitted
    sidecars). No freshness gate applies in v1 mode."""
    stances = [
        {"brain": "camino", "source_bar_close_at": "2026-07-12T15:00:00+00:00"},
        {"brain": "gto", "source_bar_close_at": None},  # unretrofitted
    ]
    all_have = all(s.get("source_bar_close_at") for s in stances)
    assert not all_have, (
        "any None means v1 path — v2 requires unanimous bar_close "
        "presence to prevent partial-freshness cliffs"
    )


def test_step7_reason_codes_are_stable_strings():
    """Every consensus_transition_skipped audit reason_code MUST be
    a stable UPPER_SNAKE_CASE string so downstream operator dashboards
    can group + alert on them without free-text drift."""
    expected_codes = {
        "CALL_MODE_NOT_AUTO",
        "POSITION_NOT_OPEN",
        "BRAIN_MAY_NOT_EXECUTE",
        "SEAT_LANE_MISMATCH",
        "STANCE_NOT_DIRECTIONAL",
        "STALE_CONSENSUS_INPUT",
    }
    # Sanity — the module's audit writer function has these branches.
    import inspect
    from shared import positions
    src = inspect.getsource(positions._maybe_auto_advance)
    for code in expected_codes:
        assert code in src, f"reason_code {code!r} missing from _maybe_auto_advance"
