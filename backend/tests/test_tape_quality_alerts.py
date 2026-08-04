"""Tape Quality Gate + Miss Alerts tests (2026-08-04)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.market_data.tape_quality import DEFAULTS, assess  # noqa: E402

pytestmark = pytest.mark.tripwire

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _tape(n=30, tf_s=60, end_offset_s=60, gap_at=None, gap_bars=0):
    """Chronological bars ending `end_offset_s` before NOW."""
    bars, t = [], NOW - timedelta(seconds=end_offset_s + (n - 1) * tf_s)
    i = 0
    while len(bars) < n:
        bars.append({"ts": t.isoformat(), "c": 100.0, "v": 10.0})
        step = tf_s * (1 + gap_bars) if (gap_at is not None
                                         and i == gap_at) else tf_s
        t += timedelta(seconds=step)
        i += 1
    return bars


def test_clean_tape_passes():
    v = assess(_tape(), now=NOW)
    assert v["ok"] and v["reason"] == "tape_ok"
    assert v["tf_sec"] == 60.0
    assert v["completeness"] == 1.0
    assert v["fingerprint"]["n"] == 30


def test_thin_tape_rejected():
    v = assess(_tape(n=5), now=NOW)
    assert not v["ok"] and v["reason"] == "thin_tape"


def test_stale_tape_rejected():
    # last bar 20 min old on a 1m tape (limit = 5×tf = 300s)
    v = assess(_tape(end_offset_s=1200), now=NOW)
    assert not v["ok"] and v["reason"] == "stale_tape"
    assert v["age_sec"] == 1200.0


def test_gappy_tape_rejected():
    v = assess(_tape(n=40, gap_at=20, gap_bars=15), now=NOW)
    assert not v["ok"] and v["reason"] == "gappy_tape"
    assert v["max_gap_bars"] == 15.0


def test_incomplete_tape_rejected():
    # many small 2-bar holes: gaps under the 12-bar cap but
    # completeness collapses below 0.95
    bars = _tape(n=60)
    holes = {5, 6, 15, 16, 25, 26, 35, 36, 45, 46}
    bars = [b for i, b in enumerate(bars) if i not in holes]
    v = assess(bars, now=NOW)
    assert not v["ok"] and v["reason"] == "incomplete_tape"
    assert v["completeness"] < 0.95


def test_five_minute_tape_inferred():
    v = assess(_tape(tf_s=300, end_offset_s=300), now=NOW)
    assert v["ok"] and v["tf_sec"] == 300.0
    # 20 min old is fine for 5m bars (limit 1500s), fatal for 1m
    v2 = assess(_tape(tf_s=300, end_offset_s=1200), now=NOW)
    assert v2["ok"]


def test_knobs_respected():
    v = assess(_tape(end_offset_s=1200),
               cfg={"max_staleness_tf_mult": 30.0}, now=NOW)
    assert v["ok"]


# ── entry timing gate integration ───────────────────────────────────

@pytest.mark.asyncio
async def test_entry_timing_blocks_bad_tape(monkeypatch):
    from shared.risk_sizer import entry_timing as et

    async def fake_cfg():
        return {"enabled": True, "profiles": {}}
    async def fake_bars(sym, limit=60):
        return _tape(end_offset_s=1200)  # stale 1m tape
    async def fake_assess(bars):
        return assess(bars, now=NOW)
    monkeypatch.setattr(et, "get_config", fake_cfg)
    monkeypatch.setattr(et, "_load_bars", fake_bars)
    monkeypatch.setattr("shared.market_data.tape_quality.assess_with_config",
                        fake_assess)
    v = await et.check_buy_entry({"symbol": "BTC/USD", "lane": "crypto"})
    assert v["allowed"] is False
    assert v["reason"] == "BAD_TAPE_QUALITY"
    assert v["decision"] == "REJECT"
    assert v["receipt"]["tape_quality"]["reason"] == "stale_tape"


def test_bad_tape_never_rearms():
    from shared.risk_sizer.entry_rearm import REARMABLE_REASONS
    assert "BAD_TAPE_QUALITY" not in REARMABLE_REASONS


# ── miss alerts ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_costly_miss_alert_written_idempotent():
    from shared.risk_sizer.missed_entries import _emit_costly_miss_alert

    writes = []

    class FakeColl:
        async def update_one(self, flt, update, upsert=False):
            writes.append((flt, update, upsert))

    class FakeDb(dict):
        def __getitem__(self, k):
            return FakeColl()

    base = {"intent_id": "i-1", "symbol": "ICNT/USD", "lane": "crypto",
            "block_reason": "entry_timing:MISSED_ENTRY_CHASE_RISK"}
    verdict = {"peak_pct": 7.2, "end_pct": 5.1}
    await _emit_costly_miss_alert(FakeDb(), base, verdict, 5.0)
    flt, update, upsert = writes[0]
    assert flt == {"_id": "alert-miss-i-1"} and upsert
    doc = update["$setOnInsert"]  # setOnInsert = idempotent re-alerts
    assert doc["kind"] == "costly_miss" and doc["acknowledged"] is False
    assert "would have hit +5% TP" in doc["message"]
    assert "MISSED_ENTRY_CHASE_RISK" in doc["message"]


def test_alert_route_registered_and_scanner_wired():
    reg = open("/app/backend/server_modules/router_registry.py").read()
    assert "routes.operator_alerts:router" in reg
    scanner = open("/app/backend/momentum/momentum_scanner.py").read()
    assert "_tape_ok" in scanner
    rearm = open("/app/backend/shared/risk_sizer/entry_rearm.py").read()
    assert "assess_with_config" in rearm
