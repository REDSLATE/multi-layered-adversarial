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


# ── dead-TTL repair (2026-07-29 audit): TTL reaps BSON Dates ONLY ──

def test_no_ttl_indexes_on_iso_string_fields():
    """The three repaired TTLs must target the BSON-Date `ttl_at`
    stamp with expireAfterSeconds=0 — never the ISO-string ts/at/
    recorded_at fields (silent no-op reaper)."""
    src = open(DB_PY).read()
    for dead in ("mc_shelly_ts_ttl_90d", "mc_parity_manifests_ttl\"",
                 "mc_brain_silences_ttl\""):
        assert f'drop_index("{dead.rstrip(chr(34))}")' in src, dead
    for live in ("mc_shelly_ttl_at", "mc_parity_manifests_ttl_at",
                 "mc_brain_silences_ttl_at"):
        assert live in src, live


def test_writers_stamp_bson_date_ttl_at():
    for path in ("/app/backend/shared/mc_shelly.py",
                 "/app/backend/mc_pulse/receipt.py",
                 "/app/backend/mc_pulse/input_manifest.py"):
        src = open(path).read()
        assert "ttl_at" in src and "timedelta" in src, path


async def test_mc_shelly_record_writes_datetime_ttl_at():
    from datetime import datetime
    from db import db
    from shared.mc_shelly import record
    await record(event_type="order_filled", brain="tripwire_probe",
                 rationale="ttl_at stamp probe")
    row = await db["mc_shelly"].find_one(
        {"brain": "tripwire_probe"}, sort=[("ts", -1)])
    assert row is not None
    assert isinstance(row.get("ttl_at"), datetime), type(row.get("ttl_at"))
    await db["mc_shelly"].delete_many({"brain": "tripwire_probe"})


# ── full TTL migration (2026-07-30, Atlas analysis #2) ──

def test_ttl_at_colls_match_retention_no_ttl_at_rules():
    """Every RULES entry the sweeper skips via the ttl_at-exists
    filter MUST have a matching {coll}_ttl_at TTL index in db.py —
    and vice versa — or stamped rows silently never expire."""
    from shared.retention import RULES, _NO_TTL_AT
    skipped = {c for c, _f, _d, e in RULES if e is _NO_TTL_AT}
    src = open(DB_PY).read()
    block = src.split("_TTL_AT_COLLS = [", 1)[1].split("]", 1)[0]
    indexed = set(re.findall(r'"([a-z_]+)"', block))
    assert skipped == indexed, (
        f"sweeper-skipped vs TTL-indexed mismatch: "
        f"only-rules={skipped - indexed} only-db={indexed - skipped}"
    )


def test_ttl_stamp_returns_bson_date():
    from datetime import datetime, timedelta, timezone
    from shared.retention import RETENTION_DAYS, ttl_stamp
    t = ttl_stamp()
    assert isinstance(t, datetime)
    delta = t - datetime.now(timezone.utc)
    assert timedelta(days=RETENTION_DAYS) - delta < timedelta(minutes=1)
