"""Public router — mounts all /public/* endpoint groups."""
from __future__ import annotations

from fastapi import APIRouter

from .agent_activity import router as agent_activity_router
from .digest import router as digest_router
from .heatmap import router as heatmap_router
from .models_mind import router as models_mind_router
from .scanner import router as scanner_router
from .signals import router as signals_router


router = APIRouter()
router.include_router(signals_router)
router.include_router(digest_router)
router.include_router(scanner_router)
router.include_router(agent_activity_router)
router.include_router(models_mind_router)
router.include_router(heatmap_router)
