"""Calibration layer (shared tooling).
Per-runtime calibrators are stored side-by-side but NEVER mixed at apply-time.
This module only manages metadata + read APIs."""
from typing import Optional
from namespaces import SHARED_CALIBRATORS
from shared.brain_identity import LEGACY_TO_CANONICAL


async def list_calibrators(db, runtime: Optional[str] = None) -> list[dict]:
    # If the caller queries by a legacy slot code (alpha/camaro/...),
    # normalize to the canonical brand ID before hitting Mongo. The
    # DB may still hold the legacy code on historical rows; we
    # therefore match on BOTH the canonical ID and any legacy alias
    # that maps to it.
    if runtime:
        canonical = LEGACY_TO_CANONICAL.get(runtime, runtime)
        legacy_aliases = [
            slot for slot, brand in LEGACY_TO_CANONICAL.items()
            if brand == canonical
        ]
        q = {"runtime": {"$in": [canonical, *legacy_aliases]}}
    else:
        q = {}
    docs = await db[SHARED_CALIBRATORS].find(q, {"_id": 0}).sort("name", 1).to_list(500)
    # 2026-06-22 — normalize the `runtime` field on the way OUT so the
    # frontend always sees canonical IDs (camino/barracuda/hellcat/gto)
    # regardless of how historical rows were tagged. The Calibration
    # page was crashing on legacy slot codes because RUNTIME_META is
    # keyed by canonical ID only. DB rows themselves are untouched —
    # legacy aliases are preserved per the doctrine pin.
    for doc in docs:
        rt = doc.get("runtime")
        if rt in LEGACY_TO_CANONICAL:
            doc["runtime"] = LEGACY_TO_CANONICAL[rt]
    return docs
