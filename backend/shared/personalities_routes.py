"""Doctrine surface — stack personalities.

Exposes the personality config so UI, MC, RoadGuard, and any future
consumer reads from the same place. No DB hits, pure config.
"""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from shared.stack_personalities import (
    STACK_PERSONALITIES,
    personality_of,
)

router = APIRouter(tags=["personalities"])


@router.get("/config/stack-personalities")
async def list_stack_personalities(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Full doctrine: all four stacks, all three layers.

    Returns:
      { "stacks": { "<stack>": { ...personality config... } } }
    """
    return {"stacks": STACK_PERSONALITIES}


@router.get("/config/stack-personalities/{stack}")
async def get_one_personality(stack: str, _user: dict = Depends(get_current_user)):  # noqa: B008
    """One stack's personality."""
    p = personality_of(stack)
    if not p:
        raise HTTPException(status_code=404, detail=f"unknown stack: {stack}")
    return {"stack": stack, **p}
