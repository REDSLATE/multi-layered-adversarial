"""Tripwire (2026-07-29): every index in `db.ensure_indexes` MUST go
through `_safe_create_index` (never raises) so one bad index spec can
never abort the rest of the run — the 2026-07-21 prod-stranding
failure mode."""
from __future__ import annotations

import re
import sys

sys.path.insert(0, "/app/backend")

DB_PY = "/app/backend/db.py"
RAW_CALL = re.compile(r"await db\.[A-Za-z_]+\.create_index\(")


def test_no_raw_create_index_calls():
    src = open(DB_PY).read()
    hits = RAW_CALL.findall(src)
    assert not hits, (
        f"{len(hits)} raw create_index call(s) in db.py — route them "
        "through _safe_create_index (fault isolation doctrine)"
    )


def test_safe_wrapper_never_raises_is_documented():
    src = open(DB_PY).read()
    assert "_safe_create_index" in src
    assert src.count("await _safe_create_index(") >= 100


async def test_safe_create_index_string_key_report_name():
    from db import _safe_create_index, db, get_index_report
    await _safe_create_index(db.tripwire_index_probe, "ts", name="probe_ts_idx")
    rep = get_index_report()
    assert "probe_ts_idx" in rep
    assert rep["probe_ts_idx"]["status"] in {"created", "exists", "timeout"}
    await db.tripwire_index_probe.drop()
