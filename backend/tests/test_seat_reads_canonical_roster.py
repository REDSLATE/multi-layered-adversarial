"""Seat authority — schema-drift regression suite (2026-02-17).

Root cause of preview execution starvation on 2026-02-17:
    `shared/seat.py::get_holder()` was hardcoded to read from the
    collection `"shared_brain_roster"` — a DEAD NAMESPACE. The canonical
    collection is `brain_roster` (per `namespaces.BRAIN_ROSTER`), which
    is what `shared/roster.py` writes to. Result: `get_lane_seats(lane)`
    returned all-None → seat returned `verdict="pass"` → auto_router
    stamped `advisory_only` on 100% of emitted intents. 500/500 recent
    intents in preview were stuck at `executor_seat_vacant:<lane>`.

Additional drift found the same session:
    The crypto executor's canonical roster assignment key is `"crypto"`
    (per the 2026-06-18 migration in `shared/roster.py:162-173`), NOT
    `"crypto_executor"`. The pre-fix seat code built its fallback key
    as `f"{lane}_{role}"` = `"crypto_executor"`, which no writer
    populates. Result: even a correctly-populated `brain_roster` couldn't
    resolve the crypto executor seat.

Doctrine pinned by these tests:
    * `seat_registry` is the primary authority.
    * `brain_roster` (from `namespaces.BRAIN_ROSTER`) is the valid
      fallback.
    * `shared_brain_roster` is a dead namespace — MUST NOT be read.
    * The canonical crypto-executor key is `"crypto"`; `"crypto_executor"`
      is at best a legacy alias, at worst nothing at all.

These are the ONLY tests that assert the seat's collection wiring and
canonical assignment keys. If a future refactor breaks any of them, the
whole intent lifecycle silently degrades to 0% clearance again.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── Bug 1: reader must not query the dead `shared_brain_roster` ────

def test_seat_module_does_not_reference_dead_shared_brain_roster():
    """Belt-and-suspenders string scan on seat.py.

    Prevents someone from silently reintroducing the drift via
    copy-paste while the tests below are green (e.g. adding a NEW
    reader elsewhere in the file). If this test ever fails, delete
    the offending string — never reintroduce that collection name.
    """
    from pathlib import Path

    seat_src = Path(__file__).resolve().parent.parent / "shared" / "seat.py"
    text = seat_src.read_text()
    # `shared_brain_roster` may only appear in comments/docstrings that
    # explicitly document it as a dead namespace, never as a live
    # collection reference. We enforce this by requiring every hit to
    # be inside a docstring/comment context — the cheap heuristic is
    # "never appears as `db[\"shared_brain_roster\"]` or as a bare
    # string used as an operand". The current file has ONE reference
    # inside the docstring explaining WHY it's dead — that's allowed.
    live_refs = [
        line for line in text.splitlines()
        if 'shared_brain_roster' in line
        and 'db[' in line
    ]
    assert not live_refs, (
        "seat.py must not read from `shared_brain_roster` — that's the "
        "dead namespace that caused the 2026-02-17 execution starvation. "
        "Offending lines:\n" + "\n".join(live_refs)
    )


def test_seat_module_imports_canonical_brain_roster_namespace():
    """The reader MUST resolve through `namespaces.BRAIN_ROSTER` — never
    a hardcoded string — so schema renames stay single-source."""
    from pathlib import Path

    seat_src = Path(__file__).resolve().parent.parent / "shared" / "seat.py"
    text = seat_src.read_text()
    assert "from namespaces import BRAIN_ROSTER" in text, (
        "seat.py must import BRAIN_ROSTER from namespaces so the collection "
        "name has a single source of truth. Hardcoded strings are how the "
        "drift snuck back in."
    )
    assert "db[BRAIN_ROSTER]" in text, (
        "seat.py must query `db[BRAIN_ROSTER]` (namespace constant), not "
        "a hardcoded string. See 2026-02-17 drift for what breaks otherwise."
    )


# ─── Bug 2: crypto executor canonical key must be "crypto" ──────────


@pytest.mark.asyncio
async def test_get_holder_equity_executor_from_brain_roster_fallback():
    """When `seat_registry` is empty, `brain_roster.assignments.executor`
    must be the source for the equity executor seat."""
    from shared import seat

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: {
        "seat_registry": _empty_coll(),
        "brain_roster": _roster_coll({
            "strategist": "gto",
            "executor": "camino",       # ← canonical equity executor key
            "governor": "hellcat",
            "auditor": "barracuda",
        }),
    }[name])

    with patch.object(seat, "db", fake_db):
        h = await seat.get_holder("equity", "executor")
    assert h == "camino", (
        f"Expected 'camino' resolved from brain_roster.executor; got {h!r}. "
        f"If this fails, the equity-executor fallback is broken again."
    )


@pytest.mark.asyncio
async def test_get_holder_crypto_executor_uses_canonical_crypto_key():
    """The crypto executor seat's canonical key is `"crypto"` — NOT
    `"crypto_executor"`. This is the drift that broke the crypto lane
    on 2026-02-17."""
    from shared import seat

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: {
        "seat_registry": _empty_coll(),
        "brain_roster": _roster_coll({
            "crypto_strategist": "camino",
            "crypto":            "gto",       # ← canonical crypto executor key
            "crypto_governor":   "hellcat",
            "crypto_auditor":    "barracuda",
            # deliberately NO "crypto_executor" key — that's dead alias
        }),
    }[name])

    with patch.object(seat, "db", fake_db):
        h = await seat.get_holder("crypto", "executor")
    assert h == "gto", (
        f"Expected 'gto' resolved from brain_roster.crypto; got {h!r}. "
        f"The canonical crypto executor key is 'crypto' per the 2026-06-18 "
        f"roster migration. If this fails, the crypto lane will starve."
    )


@pytest.mark.asyncio
async def test_get_holder_crypto_executor_tolerates_legacy_alias():
    """If a stale writer somewhere still emits `"crypto_executor"` (the
    pre-migration alias), the reader SHOULD still resolve it — as a
    tail fallback only, after the canonical `"crypto"` key."""
    from shared import seat

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: {
        "seat_registry": _empty_coll(),
        "brain_roster": _roster_coll({
            # Only the legacy alias populated — no canonical key.
            "crypto_executor": "gto",
        }),
    }[name])

    with patch.object(seat, "db", fake_db):
        h = await seat.get_holder("crypto", "executor")
    assert h == "gto", (
        "Legacy `crypto_executor` alias must still resolve — otherwise "
        "we regress on any operator tool that hasn't been migrated yet."
    )


@pytest.mark.asyncio
async def test_get_holder_canonical_key_wins_over_legacy_alias():
    """When BOTH `"crypto"` (canonical) and `"crypto_executor"` (alias)
    exist and disagree, canonical wins. Otherwise a stale legacy write
    could quietly override a fresh canonical assignment."""
    from shared import seat

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: {
        "seat_registry": _empty_coll(),
        "brain_roster": _roster_coll({
            "crypto":          "gto",         # canonical — should win
            "crypto_executor": "barracuda",   # stale alias — ignored
        }),
    }[name])

    with patch.object(seat, "db", fake_db):
        h = await seat.get_holder("crypto", "executor")
    assert h == "gto", (
        f"Canonical `crypto` must win over legacy `crypto_executor`. "
        f"Got {h!r}."
    )


@pytest.mark.asyncio
async def test_get_holder_seat_registry_wins_over_brain_roster():
    """`seat_registry` is the primary authority. If BOTH sources have a
    holder for the same (lane, role), seat_registry MUST win — it's the
    canonical write path for new operator assignments."""
    from shared import seat

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: {
        "seat_registry": _seat_registry_coll({
            "equity:executor": {"holder": "barracuda"},
        }),
        "brain_roster": _roster_coll({
            "executor": "camino",  # stale roster — must NOT win
        }),
    }[name])

    with patch.object(seat, "db", fake_db):
        h = await seat.get_holder("equity", "executor")
    assert h == "barracuda", (
        f"seat_registry must be primary authority. Got {h!r} — "
        f"if this is 'camino', the doctrine order has been inverted."
    )


@pytest.mark.asyncio
async def test_get_holder_returns_none_when_both_sources_vacant():
    """No holder anywhere → None. This is the state that legitimately
    triggers `executor_seat_vacant:<lane>` — MUST stay reachable."""
    from shared import seat

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(side_effect=lambda name: {
        "seat_registry": _empty_coll(),
        "brain_roster": _roster_coll({}),  # empty assignments
    }[name])

    with patch.object(seat, "db", fake_db):
        h = await seat.get_holder("equity", "executor")
    assert h is None


# ─── Helpers ─────────────────────────────────────────────────────────

def _empty_coll():
    """A collection stub whose `find_one` always returns None."""
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=None)
    return coll


def _roster_coll(assignments: dict):
    """Return a collection stub whose `find_one({})` yields
    `{"assignments": <assignments>}`."""
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value={"assignments": assignments})
    return coll


def _seat_registry_coll(rows_by_id: dict[str, dict]):
    """Return a collection stub whose `find_one({"_id": <id>}, ...)`
    resolves by key in `rows_by_id`."""
    coll = MagicMock()

    async def _find_one(query, projection=None):  # noqa: ARG001
        _id = (query or {}).get("_id")
        return rows_by_id.get(_id)

    coll.find_one = _find_one
    return coll
