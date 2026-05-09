"""Calibration layer (shared tooling).
Per-runtime calibrators are stored side-by-side but NEVER mixed at apply-time.
This module only manages metadata + read APIs."""
from typing import Optional
from namespaces import SHARED_CALIBRATORS


async def list_calibrators(db, runtime: Optional[str] = None) -> list[dict]:
    q = {"runtime": runtime} if runtime else {}
    return await db[SHARED_CALIBRATORS].find(q, {"_id": 0}).sort("name", 1).to_list(500)
