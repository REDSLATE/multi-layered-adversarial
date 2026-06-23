"""Artifact inventory: list of model artifacts per runtime (kept SEPARATE).
The shared layer only catalogs them; it never loads or merges across runtimes."""
from namespaces import SHARED_ARTIFACTS
from shared.brain_identity import LEGACY_TO_CANONICAL


async def list_artifacts(db, runtime: str | None = None) -> list[dict]:
    # Same shape as `shared.calibration_layer.list_calibrators` —
    # canonical IDs out, BOTH canonical + legacy aliases matched in
    # the query. See that module's docstring for the full rationale.
    if runtime:
        canonical = LEGACY_TO_CANONICAL.get(runtime, runtime)
        legacy_aliases = [
            slot for slot, brand in LEGACY_TO_CANONICAL.items()
            if brand == canonical
        ]
        q = {"runtime": {"$in": [canonical, *legacy_aliases]}}
    else:
        q = {}
    docs = await db[SHARED_ARTIFACTS].find(q, {"_id": 0}).sort([("runtime", 1), ("artifact", 1)]).to_list(500)
    for doc in docs:
        rt = doc.get("runtime")
        if rt in LEGACY_TO_CANONICAL:
            doc["runtime"] = LEGACY_TO_CANONICAL[rt]
    return docs
