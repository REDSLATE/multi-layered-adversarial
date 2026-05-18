"""ALPHA runtime — base/stable. Reads alpha_decision_log only. Never reads other runtimes' logs."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import ALPHA_DECISION_LOG

router = APIRouter(prefix="/runtime/alpha", tags=["alpha"])


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    return {
        "runtime": "alpha",
        "mode": "seat-governed",
        "decision_log_count": await db[ALPHA_DECISION_LOG].count_documents({}),
        "doctrine": (
            "alpha decisions stay in alpha_decision_log; never merged "
            "with camaro/chevelle/redeye. Execution authority is "
            "seat-bound — see /api/admin/roster."
        ),
    }


@router.get("/decisions")
async def decisions(limit: int = 50, _user: dict = Depends(get_current_user)):
    docs = await db[ALPHA_DECISION_LOG].find({}, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}
