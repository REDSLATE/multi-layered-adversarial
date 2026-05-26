"""Per-collection rollup config.

Each entry:
    {name, ts_field}
where `ts_field` is the canonical insertion-time field on rows of
that collection. MC's collections use different timestamp fields
(`ingest_ts`, `ts`, `timestamp`, `resolved_at`) — the runner reads
this map so it picks the right field.
"""
from __future__ import annotations


# Rolled-up collections + their timestamp field.
ROLLUP_COLLECTIONS: list[dict] = [
    # ── MC ──
    {"name": "shared_intents",         "ts_field": "ingest_ts"},
    {"name": "doctrine_sidecars",      "ts_field": "ts"},
    {"name": "shared_adl_receipts",    "ts_field": "timestamp"},
    {"name": "shared_brain_outcomes",  "ts_field": "resolved_at"},

    # ── Brain runtimes ──
    # These collections live in the brain sidecars' own Mongo, not
    # MC's — they're registered here so the same registry can be
    # reused from a brain-side runner. In MC, the collections will be
    # missing and the runner will report scanned=0.
    {"name": "alpha_intents",          "ts_field": "ingest_ts"},
    {"name": "alpha_receipts",         "ts_field": "timestamp"},
    {"name": "alpha_shadow_logs",      "ts_field": "ts"},
    {"name": "alpha_sidecars",         "ts_field": "ts"},

    {"name": "camaro_intents",         "ts_field": "ingest_ts"},
    {"name": "camaro_receipts",        "ts_field": "timestamp"},
    {"name": "camaro_shadow_rows",     "ts_field": "timestamp"},
    {"name": "camaro_sidecars",        "ts_field": "ts"},

    {"name": "chevelle_intents",       "ts_field": "ingest_ts"},
    {"name": "chevelle_receipts",      "ts_field": "timestamp"},
    {"name": "chevelle_shadow_logs",   "ts_field": "ts"},

    {"name": "redeye_intents",         "ts_field": "ingest_ts"},
    {"name": "redeye_receipts",        "ts_field": "timestamp"},
    {"name": "redeye_shadow_logs",     "ts_field": "ts"},
]


def collection_names() -> list[str]:
    return [c["name"] for c in ROLLUP_COLLECTIONS]
