"""Artifact inventory: list of model artifacts per runtime (kept SEPARATE).
The shared layer only catalogs them; it never loads or merges across runtimes."""
from namespaces import SHARED_ARTIFACTS


async def list_artifacts(db, runtime: str | None = None) -> list[dict]:
    q = {"runtime": runtime} if runtime else {}
    return await db[SHARED_ARTIFACTS].find(q, {"_id": 0}).sort([("runtime", 1), ("artifact", 1)]).to_list(500)
