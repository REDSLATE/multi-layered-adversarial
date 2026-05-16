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
from shared.sovereign_mode_guard import router as sovereign_router
from shared.public_api import router as public_api_router
from shared.public_api.rate_limit import (
    ensure_ttl_index as _rate_limit_ensure_ttl,
    rate_limit_middleware,
)
from shared.public_api.traffic import (
    public_traffic_middleware,
    router as public_traffic_router,
)
from shared.heartbeat_ping import router as heartbeat_ping_router
from shared.seat_performance import router as seat_performance_router
from shared.roster import router as roster_router
from shared.promotion import router as promotion_router
from shared.diagnostics import router as diagnostics_router
from shared.doctrine import router as doctrine_router
from shared.flags import router as flags_router, get_flags_snapshot
from shared.intents import router as intents_router
from shared.executor_seat import router as executor_router
from shared.auditor_seat import router as auditor_router
from shared.broker.alpaca_routes import router as alpaca_router
from shared.decisions_feed import router as decisions_router
from shared.doctrine_routes import router as doctrine_router
from shared.execution import router as execution_router
from shared.live_positions import router as live_positions_router
from shared.brain_lane_policy import router as brain_lane_policy_router, seed_default_policy
from shared.vrl import (
    router as vrl_router,
    start_scorecard_scheduler,
    stop_scorecard_scheduler,
)
from shared.quantum_routes import router as quantum_router
from shared.personalities_routes import router as personalities_router
from shared.auto_router import (
    start_auto_router_if_enabled,
    stop_auto_router,
)
from shared.hypothesis import router as hypothesis_router
from shared.mc_shelly import router as mc_shelly_router
from shared.patches import router as patches_router
from shared.public_api.news import (
    router as public_news_router,
    start_news_refresher,
    stop_news_refresher,
)
from shared.public_api.dark_pool import (
    router as public_darkpool_router,
    start_darkpool_refresher,
    stop_darkpool_refresher,
)
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
    # Auto-router — picks up council-approved intents and submits them to
    # the broker without operator clicks. Paper trading only; gated by
    # the same gate chain as /execution/submit.
    alpaca_doc = await db["alpaca_credentials"].find_one({"_id": "singleton"}, {"_id": 1})
    if alpaca_doc:
        start_auto_router_if_enabled()
        logger.info("Auto-router started")
    # Public-API rate-limit collection — TTL index for buckets.
    await _rate_limit_ensure_ttl()
    # Public news + dark-pool refreshers — fail-soft proxies to base44.
    start_news_refresher()
    logger.info("Public news refresher started")
    start_darkpool_refresher()
    logger.info("Public dark-pool refresher started")
    # VRL nightly scorecard recomputer — opt-out via VRL_SCHEDULER_ENABLED=false.
    start_scorecard_scheduler()
    logger.info("VRL scorecard scheduler started")
    # Seed default brain × lane emission policy (idempotent).
    try:
        await seed_default_policy()
        logger.info("Brain × lane emission policy seeded")
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_lane_policy seed failed: %s", e)
    yield
    await stop_poller()
    await stop_tickler()
    await stop_public_refresher()
    await stop_auto_router()
    await stop_news_refresher()
    await stop_darkpool_refresher()
    await stop_scorecard_scheduler()
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
    # Doctrine (2026-02-16): deploy_mode reports OBSERVABLE STATE, not a
    # configuration label. If any connected broker has execution_enabled
    # = True, the system is functionally in execution mode. The env var
    # is now a *floor* — if DEPLOY_MODE=execution is set, we trust it;
    # otherwise we derive. This fixes the long-running cosmetic bug
    # where prod showed "observation" while live trading was active.
    env_mode = os.environ.get("DEPLOY_MODE", "observation").lower()
    derived_mode = "observation"
    if mongo_ok:
        try:
            alpaca_exec = await db["alpaca_credentials"].find_one(
                {"_id": "singleton"}, {"_id": 0, "execution_enabled": 1},
            )
            kraken_exec = await db["kraken_credentials"].find_one(
                {"_id": "singleton"}, {"_id": 0, "execution_enabled": 1},
            )
            if (alpaca_exec and alpaca_exec.get("execution_enabled")) or \
               (kraken_exec and kraken_exec.get("execution_enabled")):
                derived_mode = "execution"
        except Exception:  # noqa: BLE001
            pass
    # If either source says "execution", report execution.
    deploy_mode = "execution" if env_mode == "execution" or derived_mode == "execution" else "observation"
    return {
        "ok": True,
        "mongo": mongo_ok,
        "deploy_mode": deploy_mode,
        "deploy_mode_env": env_mode,
        "deploy_mode_derived": derived_mode,
    }


# Mount sub-routers
api_router.include_router(auth_router)
api_router.include_router(shared_router)
api_router.include_router(ingest_router)
api_router.include_router(opinions_router)
api_router.include_router(outcomes_router)
api_router.include_router(conflicts_router)
api_router.include_router(positions_router)
api_router.include_router(sovereign_router)
api_router.include_router(public_api_router)
api_router.include_router(public_traffic_router)
api_router.include_router(heartbeat_ping_router)
api_router.include_router(seat_performance_router)
api_router.include_router(technicals_router)
api_router.include_router(kraken_router)
api_router.include_router(ibkr_router)
api_router.include_router(public_router)
api_router.include_router(roster_router)
api_router.include_router(promotion_router)
api_router.include_router(doctrine_router)
api_router.include_router(intents_router)
api_router.include_router(executor_router)
api_router.include_router(auditor_router)
api_router.include_router(alpaca_router)
api_router.include_router(execution_router)
api_router.include_router(live_positions_router)
api_router.include_router(brain_lane_policy_router)
api_router.include_router(vrl_router)
api_router.include_router(hypothesis_router)
api_router.include_router(mc_shelly_router)
api_router.include_router(patches_router)
api_router.include_router(public_news_router)
api_router.include_router(public_darkpool_router)
api_router.include_router(diagnostics_router)
api_router.include_router(decisions_router)
api_router.include_router(doctrine_router)
api_router.include_router(quantum_router)
api_router.include_router(personalities_router)
api_router.include_router(flags_router)
api_router.include_router(alpha_router)
api_router.include_router(camaro_router)
api_router.include_router(chevelle_router)

app.include_router(api_router)

# Public-API middleware stack.
# Starlette runs `middleware("http")` in REVERSE order — last added is
# outermost. We want:
#   outermost: traffic logger  → sees the final response (incl. 429s)
#   inner:     rate limiter    → can short-circuit with 429
# So we add the rate limiter FIRST (inner) and the traffic logger LAST
# (outer). Don't reorder these without re-reading this comment.
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(public_traffic_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
