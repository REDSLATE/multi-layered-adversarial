"""
RISEDUAL Monorepo Backend — Mission Control
Shared infrastructure + isolated runtimes (Alpha, Camaro, Chevelle, REDEYE).

Deploy posture: SEAT-GOVERNED — execution authority lives in the seat
policy + execution gate. Brains propose; MC regulates at the gate.

Entry point only. The big pieces live in `server_modules/`:
    - lifespan.py          — boot migrations, worker start/stop, shutdown
    - router_registry.py   — every `api_router.include_router(...)` call
    - middleware_setup.py  — CORS + public-API middleware
    - meta_routes.py       — `/`, `/health`, `/admin/neutral-brains/status`

Refactored 2026-06-18. Behavior is 1:1 with the pre-refactor monolith.
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import logging

from fastapi import APIRouter, FastAPI

from server_modules.lifespan import lifespan
from server_modules.meta_routes import router as meta_router
from server_modules.middleware_setup import setup_middleware
from server_modules.router_registry import register_routers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = FastAPI(title="RISEDUAL Mission Control", lifespan=lifespan)

api_router = APIRouter(prefix="/api")
api_router.include_router(meta_router)
register_routers(api_router)
app.include_router(api_router)

setup_middleware(app)
