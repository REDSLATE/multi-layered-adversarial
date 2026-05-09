"""CHEVELLE runtime. Reads chevelle_memory_labels only."""
import os
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import CHEVELLE_MEMORY_LABELS

router = APIRouter(prefix="/runtime/chevelle", tags=["chevelle"])


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    return {
        "runtime": "chevelle",
        "mode": "observation",
        "authority_enabled": os.environ.get("CHEVELLE_AUTHORITY_ENABLED", "false").lower() == "true",
        "memory_labels_count": await db[CHEVELLE_MEMORY_LABELS].count_documents({}),
        "doctrine": "chevelle authority disabled in observation mode; calls remain advisory only",
    }


@router.get("/memory-labels")
async def memory_labels(limit: int = 50, _user: dict = Depends(get_current_user)):
    docs = await db[CHEVELLE_MEMORY_LABELS].find({}, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}
