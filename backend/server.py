"""
RISEDUAL Monorepo Backend — Mission Control
Shared infrastructure + isolated runtimes (Alpha, Camaro, Chevelle).
Deploy posture: OBSERVATION ONLY — no live broker execution, no model authority merging.
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware

from db import db, client, ensure_indexes
from auth import router as auth_router, seed_admin
from shared.routes import router as shared_router
from shared.ingest import router as ingest_router
from shared.opinions import router as opinions_router
from shared.outcomes import router as outcomes_router
from shared.conflicts import router as conflicts_router
from shared.technicals import router as technicals_router
from shared.kraken_routes import router as kraken_router, start_poller_if_needed, stop_poller
from shared.ibkr import router as ibkr_router, start_tickler_if_needed, stop_tickler
from shared.public import router as public_router, start_refresher_if_needed as start_public_refresher, stop_refresher as stop_public_refresher
from shared.positions import router as positions_router
from shared.heartbeat_ping import router as heartbeat_ping_router
from shared.roster import router as roster_router
from shared.promotion import router as promotion_router
from shared.diagnostics import router as diagnostics_router
from shared.flags import router as flags_router, get_flags_snapshot
from shared.seed import seed_all
from runtimes.alpha.routes import router as alpha_router
from runtimes.camaro.routes import router as camaro_router
from runtimes.chevelle.routes import router as chevelle_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("risedual")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()
    await seed_admin(db)
    await seed_all(db)
    flags = get_flags_snapshot()
    logger.info("RISEDUAL boot: deploy_mode=%s flags=%s", flags["deploy_mode"], flags["enforce_flags"])
    # Start the Kraken auto-poller if credentials exist. Safe no-op when
    # nothing is configured — the loop short-circuits on empty doc.
    kraken_doc = await db["kraken_credentials"].find_one({"_id": "singleton"}, {"_id": 1})
    if kraken_doc:
        start_poller_if_needed()
        logger.info("Kraken auto-poller started")
    ibkr_doc = await db["ibkr_credentials"].find_one({"_id": "singleton"}, {"_id": 1})
    if ibkr_doc:
        start_tickler_if_needed()
        logger.info("IBKR tickler started")
    public_doc = await db["public_credentials"].find_one({"_id": "singleton"}, {"_id": 1})
    if public_doc:
        start_public_refresher()
        logger.info("Public.com token refresher started")
    yield
    await stop_poller()
    await stop_tickler()
    await stop_public_refresher()
    client.close()


app = FastAPI(title="RISEDUAL Mission Control", lifespan=lifespan)

api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {
        "name": "RISEDUAL Mission Control",
        "deploy_mode": os.environ.get("DEPLOY_MODE", "observation"),
        "runtimes": ["alpha", "camaro", "chevelle"],
        "doctrine": "one shared nervous system, three separate decision brains",
    }


@api_router.get("/health")
async def health():
    try:
        await client.admin.command("ping")
        mongo_ok = True
    except Exception:  # noqa: BLE001
        mongo_ok = False
    return {"ok": True, "mongo": mongo_ok, "deploy_mode": os.environ.get("DEPLOY_MODE", "observation")}


# Mount sub-routers
api_router.include_router(auth_router)
api_router.include_router(shared_router)
api_router.include_router(ingest_router)
api_router.include_router(opinions_router)
api_router.include_router(outcomes_router)
api_router.include_router(conflicts_router)
api_router.include_router(positions_router)
api_router.include_router(heartbeat_ping_router)
api_router.include_router(technicals_router)
api_router.include_router(kraken_router)
api_router.include_router(ibkr_router)
api_router.include_router(public_router)
api_router.include_router(roster_router)
api_router.include_router(promotion_router)
api_router.include_router(diagnostics_router)
api_router.include_router(flags_router)
api_router.include_router(alpha_router)
api_router.include_router(camaro_router)
api_router.include_router(chevelle_router)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
