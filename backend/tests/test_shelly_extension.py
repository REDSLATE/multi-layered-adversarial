"""L3 verified facts + L6 wiki + MEMORY.md tests.

Uses the live preview Mongo connection. Tests are idempotent (each
seeds its own data with deterministic event_hash values and reads
those back). No mock kernel — these exercise the actual Mongo path
via the sync_db getter shared with Shelly.
"""
from __future__ import annotations

from typing import Any

import pytest

from shelly.contracts import ShellyMemoryEvent
from shelly.local_shelly import LocalShelly
from shelly.memory_profile import render_brain_memory_md
from shelly.sync_db import get_db
from shelly.verified_facts import (
    VERIFIED_FACTS_COLL,
    WIKI_COLL,
    auto_certify_scan,
    certify_one,
    curate_wiki_run,
    verified_facts_summary,
    wiki_lookup,
    wiki_summary,
)


SHARED_COLL = "shelly_mc_shared_memory"


def _seed_shared_event(symbol: str, direction: str, brains: list[str],
                       *, resolved: bool = True, pnl_pct: float = 0.01) -> str:
    """Insert N rollup rows (one per brain) for the same event content
    so they share an event_hash. Returns the hash."""
    db = get_db()
    db[SHARED_COLL].delete_many({"symbol": symbol, "direction": direction})
    event = ShellyMemoryEvent(
        brain="seed", symbol=symbol, direction=direction,
        confidence=0.7, decision="trust",
        features={"regime": "test"},
        outcome=({"pnl_pct": pnl_pct} if resolved else None),
    )
    doc = event.to_doc()
    h = doc["event_hash"]
    for b in brains:
        d = dict(doc)
        d.pop("brain", None)
        d["source_brain"] = b
        d["shelly_scope"] = "mc_shared"
        db[SHARED_COLL].update_one(
            {"event_hash": h, "source_brain": b},
            {"$setOnInsert": d},
            upsert=True,
        )
    return h


def _wipe_facts_and_wiki(symbol: str):
    db = get_db()
    db[VERIFIED_FACTS_COLL].delete_many({"symbol": symbol})
    db[WIKI_COLL].delete_many({"symbol": symbol})


# ──────────────────────── L3: verified facts ────────────────────────


def test_certify_one_missing_hash_returns_no_shared_memory_for_hash():
    out = certify_one("does_not_exist_hash")
    assert out["ok"] is False
    assert out["reason"] == "no_shared_memory_for_hash"


def test_certify_one_writes_fact_idempotently():
    sym = "L3_TEST_AAA"
    _wipe_facts_and_wiki(sym)
    h = _seed_shared_event(sym, "BUY", brains=["alpha"])

    r1 = certify_one(h, via="operator", operator="test@x")
    assert r1["ok"] is True
    assert r1.get("newly_verified") is True

    r2 = certify_one(h, via="operator")
    assert r2["ok"] is True
    assert r2.get("already_verified") is True

    _wipe_facts_and_wiki(sym)


def test_auto_certify_promotes_when_three_brains_converge():
    sym = "L3_TEST_BBB"
    _wipe_facts_and_wiki(sym)
    h = _seed_shared_event(sym, "BUY", brains=["alpha", "camaro", "redeye"])

    out = auto_certify_scan(limit=200)
    assert out["ok"] is True
    # Our seeded hash must be among newly_verified OR already_verified.
    facts = get_db()[VERIFIED_FACTS_COLL].find_one(
        {"event_hash": h}, {"_id": 0, "via": 1}
    )
    assert facts is not None
    assert facts["via"] in ("auto_convergence", "operator")

    _wipe_facts_and_wiki(sym)


def test_auto_certify_does_not_promote_below_convergence():
    sym = "L3_TEST_CCC"
    _wipe_facts_and_wiki(sym)
    h = _seed_shared_event(sym, "SELL", brains=["alpha", "camaro"])  # only 2

    auto_certify_scan(limit=200)
    facts = get_db()[VERIFIED_FACTS_COLL].find_one(
        {"event_hash": h, "via": "auto_convergence"}, {"_id": 0}
    )
    assert facts is None

    _wipe_facts_and_wiki(sym)


def test_verified_facts_summary_returns_counts():
    s = verified_facts_summary()
    assert s["ok"] is True
    assert "total" in s
    assert "by_via" in s


# ──────────────────────── L6: RISEDUAL wiki ────────────────────────


def test_curate_wiki_run_writes_topic_entry():
    sym = "L6_TEST_DDD"
    _wipe_facts_and_wiki(sym)
    h = _seed_shared_event(sym, "BUY", brains=["alpha", "camaro", "redeye"],
                            resolved=True, pnl_pct=0.05)
    certify_one(h, via="operator")

    out = curate_wiki_run(limit=500)
    assert out["ok"] is True

    entries = wiki_lookup(sym, "BUY")
    assert len(entries) == 1
    e = entries[0]
    assert e["topic_key"] == f"{sym}::BUY"
    assert e["summary"]["n_facts"] >= 1
    assert e["summary"]["wins"] >= 1

    _wipe_facts_and_wiki(sym)


def test_curate_wiki_is_idempotent_overwrites_row():
    sym = "L6_TEST_EEE"
    _wipe_facts_and_wiki(sym)
    h = _seed_shared_event(sym, "BUY", brains=["alpha", "camaro", "redeye"])
    certify_one(h, via="operator")
    curate_wiki_run(limit=500)
    n1 = len(wiki_lookup(sym, "BUY"))
    curate_wiki_run(limit=500)
    n2 = len(wiki_lookup(sym, "BUY"))
    assert n1 == 1 and n2 == 1   # upsert, not duplicate

    _wipe_facts_and_wiki(sym)


def test_wiki_summary_returns_total():
    out = wiki_summary()
    assert out["ok"] is True
    assert "total_entries" in out


# ──────────────────────── MEMORY.md ────────────────────────


def test_memory_md_renders_for_known_brain():
    md = render_brain_memory_md("alpha", recent_limit=5)
    assert isinstance(md, str)
    assert md.startswith("# MEMORY.md — ALPHA")
    assert "Authority:" in md
    # Resilient even with zero memories — section headers still appear.
    assert "## Totals" in md
    assert "## Recent events" in md


def test_memory_md_renders_with_seeded_event():
    brain = "redeye"
    ls = LocalShelly(brain)
    ev = ShellyMemoryEvent(
        brain=brain, symbol="MEMTEST", direction="SELL",
        confidence=0.6, decision="modulate",
        features={"regime": "wide_spread"},
        mc_status="WOULD_PASS", roadguard_status="OK",
        outcome={"pnl_pct": -0.02},
    )
    ls.remember(ev)

    md = render_brain_memory_md(brain, recent_limit=20)
    # Don't assert specific row text (might be filtered or differ);
    # rather assert the symbol shows up somewhere in the rendering.
    assert "MEMTEST" in md

    # Cleanup
    ls.memories.delete_many({"symbol": "MEMTEST"})


def test_memory_md_rejects_unknown_brain_at_renderer_level_returns_empty_block():
    """The renderer itself does not validate the brain name (that's the
    route layer's job — it returns 400). Render still returns a doc."""
    md = render_brain_memory_md("ghost", recent_limit=3)
    assert "GHOST" in md
