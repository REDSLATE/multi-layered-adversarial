"""Operator-only diagnostic: sanitized live account snapshot per lane.
Reads live broker state via the same adapter resolution the live route
uses. Admin auth required — never expose publicly.
"""
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.account_context import get_account_snapshot

router = APIRouter(prefix="/admin/account-context", tags=["admin-account-context"])


@router.get("/{lane}")
async def account_context(
    lane: str,
    force: bool = Query(False),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    try:
        snap = await get_account_snapshot(lane, force=force)
    except RuntimeError as exc:
        return {"ok": False, "lane": lane, "error": str(exc)}
    return {"ok": True, **snap.model_payload()}
