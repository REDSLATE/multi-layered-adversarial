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


def test_every_retention_rule_has_an_index_spec():
    """2026-07-29 (Atlas analysis #1): every retention.RULES sweep
    field must have a matching entry in db.ensure_indexes'
    _RETENTION_FIELDS — otherwise the hourly purge collscans and
    holds a pool socket (the 2026-07-16 login-starvation mechanism)."""
    from shared.retention import RULES
    src = open(DB_PY).read()
    missing = [
        (coll, field) for coll, field, _d, _e in RULES
        if f'("{coll}", "{field}")' not in src
    ]
    assert not missing, f"retention fields without index specs: {missing}"


def test_retention_uses_capped_worker_pool():
    """2026-07-29 (Atlas analysis #4): the sweeper must run on the
    dedicated capped-pool worker client, never the shared client."""
    import shared.retention as retention
    from db import worker_db
    assert retention.db is worker_db
