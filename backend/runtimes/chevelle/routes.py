"""CHEVELLE runtime. Reads chevelle_memory_labels only."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import CHEVELLE_MEMORY_LABELS

router = APIRouter(prefix="/runtime/chevelle", tags=["hellcat"])


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    return {
        "runtime": "hellcat",
        "mode": "seat-governed",
        "memory_labels_count": await db[CHEVELLE_MEMORY_LABELS].count_documents({}),
        "doctrine": (
            "chevelle memory labels stay in chevelle_memory_labels. "
            "Execution + veto authority is seat-bound — see "
            "/api/admin/roster."
        ),
    }


@router.get("/memory-labels")
async def memory_labels(limit: int = 50, _user: dict = Depends(get_current_user)):
    docs = await db[CHEVELLE_MEMORY_LABELS].find({}, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}
