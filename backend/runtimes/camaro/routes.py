"""CAMARO runtime. Reads camaro_shadow_rows only."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from namespaces import CAMARO_SHADOW_ROWS

router = APIRouter(prefix="/runtime/camaro", tags=["barracuda"])


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    return {
        "runtime": "barracuda",
        "mode": "seat-governed",
        "shadow_rows_count": await db[CAMARO_SHADOW_ROWS].count_documents({}),
        "doctrine": (
            "camaro shadow rows stay in camaro_shadow_rows. Execution "
            "authority is seat-bound — see /api/admin/roster."
        ),
    }


@router.get("/shadow-rows")
async def shadow_rows(limit: int = 50, _user: dict = Depends(get_current_user)):
    docs = await db[CAMARO_SHADOW_ROWS].find({}, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}
