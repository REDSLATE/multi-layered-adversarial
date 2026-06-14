"""
RISEDUAL Monorepo Backend — Mission Control
Shared infrastructure + isolated runtimes (Alpha, Camaro, Chevelle, REDEYE).
Deploy posture: SEAT-GOVERNED — execution authority lives in the seat
policy + execution gate. Brains propose; MC regulates at the gate.
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
from shared.crypto.routes import router as kraken_router, start_poller_if_needed, stop_poller
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
from shared.doctrine import scorecard_router as doctrine_scorecard_router
from shared.doctrine import auto_retire_router as doctrine_auto_retire_router
from shared.doctrine import promotion_router as doctrine_promotion_router
from shared.flags import router as flags_router, get_flags_snapshot
from shared.intents import router as intents_router
from shared.executor_seat import router as executor_router
from shared.auditor_seat import router as auditor_router
from shared.seat_nudges import router as seat_nudges_router
from shared.decisions_feed import router as decisions_router
from shared.doctrine_routes import router as doctrine_router
from shared.execution import router as execution_router
from shared.live_positions import router as live_positions_router
from shared.brain_lane_policy import router as brain_lane_policy_router, seed_default_policy
from shared.redeye_crypto_intent_bridge import router as redeye_bridge_router
from shared.risk.routes import router as risk_router
from shared.risk.position_monitor import (
    start_monitor_if_enabled as start_position_monitor,
    stop_monitor as stop_position_monitor,
)
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
from shared.runtime_tokens import router as runtime_tokens_router
from shared.runtime.routes import router as platform_survival_router
from shared.runtime.sidecar_checkin import router as sidecar_checkin_router
from shared.calibration.confidence_floor_sweep import router as confidence_floor_sweep_router
from shared.calibration.snapshot_completeness import router as snapshot_completeness_router
from routes.memory_kernel_routes import router as memory_kernel_router
from routes.orphan_inspection_routes import router as orphan_inspection_router
from routes.orphan_replay_routes import router as orphan_replay_router
from routes.broker_freeze_routes import router as broker_freeze_router
from routes.broker_reconcile_routes import router as broker_reconcile_router
from routes.sidecar_diagnostics import router as sidecar_diagnostics_router
from routes.data_stack_admin import router as data_stack_admin_router
from routes.market_data_keys import router as market_data_keys_router
from routes.opinion_silence_watchdog import router as opinion_silence_watchdog_router
from routes.heartbeat_reconciler_admin import router as heartbeat_reconciler_admin_router
from routes.brain_outages import router as brain_outages_router
from routes.brain_health import router as brain_health_router
from routes.market_data_snapshot import router as market_data_snapshot_router
from routes.brain_runtime import router as brain_runtime_router
from routes.daily_snapshots import router as daily_snapshots_router
from shared.snapshots.service import (
    ensure_indexes as ensure_daily_snapshot_indexes,
)
from shared.snapshots.worker import (
    start_worker_if_enabled as start_daily_snapshot_worker,
    stop_worker as stop_daily_snapshot_worker,
)
from shared.feeders.finnhub_equity import (
    start_worker_if_enabled as start_finnhub_worker,
    stop_worker as stop_finnhub_worker,
)
from shared.feeders.polygon_equity import (
    start_worker_if_enabled as start_polygon_worker,
    stop_worker as stop_polygon_worker,
)
from shared.alt_data.sec_edgar import (
    start_worker_if_enabled as start_sec_edgar_worker,
    stop_worker as stop_sec_edgar_worker,
)
from shared.alt_data.fred import (
    start_worker_if_enabled as start_fred_worker,
    stop_worker as stop_fred_worker,
)
from shelly import router as shelly_router
from routes.brain_memory_ingest import router as brain_memory_ingest_router
from routes.paradox_routes import router as paradox_router
from routes.paradox_agent_routes import router as paradox_agent_router
from routes.paradox_wake_routes import router as paradox_wake_router
from routes.llm_ledger_routes import router as llm_ledger_router
from routes.paradox_watchlist_routes import router as paradox_watchlist_router
from routes.ai_run_routes import router as ai_run_router
from routes.rise_ai_threads_routes import router as rise_ai_threads_router
from routes.brain_emission_diagnose import router as brain_emission_diagnose_router
from routes.seat_registry_diagnose import router as seat_registry_diagnose_router
from routes.rise_ai_admin import router as rise_ai_admin_router
from routes.shelly_admin_extension import router as shelly_admin_extension_router
from routes.sidecar_imposter_scan import router as sidecar_imposter_scan_router
from shared.shelly_bus.mc_shelly_ingest import router as shelly_bus_router
from routes.brain_doctrine_hint import router as brain_doctrine_hint_router
from shared.observation_receipts import router as observation_receipts_router
from shared.learning_ladder import router as learning_ladder_router
from routes.intent_inspect import router as intent_inspect_router
from routes.storage_rollup import router as storage_rollup_router
from routes.trading_controls import router as trading_controls_router
from routes.runtime_token_health import router as runtime_token_health_router
from routes.alpha_vantage_admin import router as alpha_vantage_admin_router
from routes.broker_lane_admin import router as broker_lane_admin_router
from routes.auto_router_admin import router as auto_router_admin_router
from routes.broker_fills_admin import router as broker_fills_admin_router
from routes.intent_summary import router as intent_summary_router
from routes.mc_connection_stream import router as mc_connection_stream_router
from routes.position_misread_admin import router as position_misread_admin_router
from routes.intent_origin import router as intent_origin_router
from routes.webull_admin import router as webull_admin_router
from routes.admin_wrappers import router as admin_wrappers_router
from routes.admin_intents_post_mortem import router as admin_intents_post_mortem_router
from routes.admin_auto_submit import router as admin_auto_submit_router
from routes.parabolic_phase_admin import router as parabolic_phase_admin_router
from routes.data_council_admin import router as data_council_admin_router
from routes.broker_selection import router as broker_selection_router
from routes.strategy_reference import router as strategy_reference_router
from routes.doctrine_training_export import router as doctrine_training_router
from routes.doctrine_eval import router as doctrine_eval_router
from routes.outcome_join_admin import router as outcome_join_admin_router
from routes.shadow_outcome_admin import router as shadow_outcome_admin_router
from routes.scorecard_by_brain import router as scorecard_by_brain_router
from routes.safety_gates_audit import router as safety_gates_audit_router
from routes.paradox_v2 import router as paradox_v2_router



from shared.lane_execution import router as lane_execution_router
from shared.coordinator.routes import router as coordinator_router
from shared.coordinator.lifespan import (
    start_paradox_coordinator,
    stop_paradox_coordinator,
)
from shared.coordinator.user_seed import ensure_coordinator_user
from shared.observation_resolver import (
    start_observation_resolver,
    stop_observation_resolver,
)

from shared.runtime_bundles import router as runtime_bundles_router
from shared.promotion_artifact_report import router as promotion_artifact_report_router
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
    # 2026-02-19 (prod incident): bump the asyncio default thread pool
    # so blocking work (Webull SDK, bcrypt password verification,
    # synchronous Mongo migration helpers) doesn't starve UNRELATED
    # async work like /api/auth/login. Python's default is
    # `min(32, cpu_count + 4)` which on small pods is 5-8 threads —
    # not enough headroom when the brain runner fans 4 brains × ~50
    # symbols out to Webull per tick. 64 threads is cheap (each idle
    # thread is ~8KB stack) and gives the operator a fighting chance
    # to log in even when Webull is hung. Combined with the
    # circuit breaker on webull_quotes.py this is belt + suspenders.
    import asyncio  # noqa: WPS433
    from concurrent.futures import ThreadPoolExecutor  # noqa: WPS433
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=64, thread_name_prefix="risedual-io"),
    )
    logger.info("asyncio default executor set to 64-thread pool")

    await ensure_indexes()
    await seed_admin(db)
    await seed_all(db)
    # Paradox v2 — idempotent seed of brains, seat policies, governor
    # rules, and the alpha→equity_executor trust default. Stand-alone
    # deployment: not wired into the live intent flow yet (operator
    # exercises it via /api/v2/evaluate).
    try:
        from shared.paradox_v2.seed import seed_paradox_v2
        v2_seed = await seed_paradox_v2()
        logger.info("Paradox v2 seed: %s", v2_seed.get("seeded"))
    except Exception as e:  # noqa: BLE001
        logger.warning("Paradox v2 seed failed (non-fatal): %s", e)

    # Auto-submit policy — hydrate persisted override from Mongo so
    # the operator's toggle survives pod restarts. Without this,
    # `_POLICY_OVERRIDE` resets to {} on every boot and Shelly
    # silently forgets she was enabled (2026-02-19 prod incident
    # — "I flipped the toggle and nothing happened").
    try:
        from shared.auto_submit_policy import hydrate_from_mongo as _hydrate_auto_submit
        p = await _hydrate_auto_submit()
        logger.info(
            "auto_submit_policy boot: enabled=%s · source=%s",
            p.get("enabled"), p.get("source"),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("auto_submit_policy: lifespan hydrate FAILED: %s", e)

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
        # ── Broker fills ingestor (operator directive, 2026-06-10) ──
        # Polls Public's /history every 20s and upserts canonical fill
        # rows into shared_broker_fills. Closes the AAPL-incident
        # broker-amnesia gap — auto-router dedupe (next pass) reads
        # from this collection to know what's in flight.
        try:
            from shared.broker_fills import start_broker_fills_poller
            start_broker_fills_poller()
            logger.info("Public.com broker_fills poller started")
        except Exception as e:  # noqa: BLE001
            logger.warning("broker_fills poller start failed: %s", e)
    # Auto-router — picks up council-approved intents and submits them to
    # the broker without operator clicks. Gated by the same gate chain
    # as /execution/submit.
    #
    # Doctrine pin (2026-02-19, rev 2): the unconditional auto-router
    # start crashed the prod pod (HTTP 520 across all authed endpoints
    # ~30s after boot). Root cause to be confirmed, but most likely
    # candidates: (a) Webull adapter blocking the event loop with a
    # sync HTTP call when 30+ queued intents got picked up at once,
    # (b) connection-pool exhaustion against MongoDB during the first
    # _tick, (c) log volume from per-intent exceptions OOM-killing
    # the pod.
    #
    # Until the offender is identified, the auto-router is gated on
    # an EXPLICIT, OPERATOR-FLIPPED FLAG:
    #     /admin/auto-router/start  POST  → flips the gate ON in
    #                                       `runtime_flags` collection
    # The flag persists across pod restarts. Operator can flip it
    # back OFF if the pod degrades again. This is safer than the
    # previous all-or-nothing env var because the operator can
    # iterate without redeploying.
    enabled_flag = await db["runtime_flags"].find_one(
        {"_id": "auto_router_enabled"}, {"_id": 0, "enabled": 1}
    )
    if enabled_flag and enabled_flag.get("enabled") is True:
        try:
            start_auto_router_if_enabled()
            logger.info("Auto-router started (runtime_flags.auto_router_enabled=true)")
        except Exception as e:  # noqa: BLE001
            logger.error("Auto-router start failed: %s", e)
    else:
        logger.info(
            "Auto-router NOT started — runtime_flags.auto_router_enabled is not true. "
            "POST /api/admin/auto-router/start to enable."
        )
    # Keep Alpaca's pinger conditional — only matters if Alpaca creds
    # exist (zero-cost no-op otherwise).
    # 2026-02-19: Alpaca pinger removed (Alpaca broker fully deprecated).
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
    # Position Monitor loop — periodic risk-guard evaluation
    # (StopLoss → TakeProfit → TrailingStop → MaxHoldTime).
    try:
        start_position_monitor()
        logger.info("Position Monitor started")
    except Exception as e:  # noqa: BLE001
        logger.warning("position_monitor start failed: %s", e)
    # Orphan watchdog — REMOVED 2026-02-19 along with Alpaca deprecation.
    # The orphan-fill class only existed because pre-iter-106m Camaro
    # bypassed MC and POSTed direct to Alpaca. With Alpaca gone and MC
    # receipt sealing enforced on the Webull path, no orphan ingress
    # surface remains.
    # PARADOX coordinator — in-process agent scheduler. Every agent
    # starts DISABLED; operator opts in per agent via
    # `/api/admin/coordinator/enable/{agent}`.
    try:
        await ensure_coordinator_user()
        await start_paradox_coordinator()
    except Exception as e:  # noqa: BLE001
        logger.warning("paradox_coordinator start failed: %s", e)
    # Observation Resolver — Phase 2 of ladder doctrine. Grades
    # observation receipts against market price at +1h/+4h/+1d/+5d
    # horizons. Read-only on brokers; safe even without execution.
    try:
        await start_observation_resolver()
        logger.info("Observation resolver started")
    except Exception as e:  # noqa: BLE001
        logger.warning("observation_resolver start failed: %s", e)
    # Opinion Resolver — auto-grades directional opinions (long/short)
    # against market price after a configurable horizon (default 24h).
    # Writes to shared_brain_outcomes with resolved_by="auto:market-data".
    # 2026-05-24: built to close the 458/485-operator-driven gap.
    try:
        from shared.opinion_resolver import start_worker as _start_opinion_resolver
        _start_opinion_resolver()
        logger.info("Opinion resolver started")
    except Exception as e:  # noqa: BLE001
        logger.warning("opinion_resolver start failed: %s", e)
    # Data Stack Phase 1 — Finnhub equity OHLCV, SEC EDGAR Form 4
    # filings index, and FRED macro series. Each worker is a no-op
    # unless its `*_ENABLED=true` env-var is set; missing API keys
    # produce one feeder_health_audit row and the worker idles.
    try:
        start_finnhub_worker()
        start_polygon_worker()
        start_sec_edgar_worker()
        start_fred_worker()
    except Exception as e:  # noqa: BLE001
        logger.warning("data_stack workers start failed: %s", e)
    # Opinion-silent watchdog — autonomous scan that emits an alert
    # row when any occupied seat goes > threshold without an opinion
    # POST. Advisory observability only. Doctrine pin:
    # `shared/runtime/opinion_silence_worker.py`.
    try:
        from shared.runtime.opinion_silence_worker import (
            start_worker as _start_opinion_silence_worker,
        )
        _start_opinion_silence_worker()
        logger.info("Opinion-silent watchdog started")
    except Exception as e:  # noqa: BLE001
        logger.warning("opinion_silence_worker start failed: %s", e)

    # 2026-02-20 — Heartbeat reconciler.
    # Periodically derives `shared_heartbeats.last_seen` from
    # `sidecar_checkin_audit` so the LIVE/STALE/DEAD badge can't
    # drift out of sync with the imposter scan even when the
    # per-request side-effect in sidecar_checkin.py silently
    # fails (e.g., transient Mongo write blip).
    try:
        from shared.runtime.heartbeat_reconciler import (
            start_worker as _start_heartbeat_reconciler,
        )
        _start_heartbeat_reconciler()
        logger.info("Heartbeat reconciler started")
    except Exception as e:  # noqa: BLE001
        logger.warning("heartbeat_reconciler start failed: %s", e)
    # Shadow-close cron — auto-fires `run_shadow_close` at 4:05pm ET
    # every weekday so the LEARNING counter ticks without an operator
    # click. Idempotent (per-ET-day + the existing `outcome_join`
    # `$exists: false` guard) so a slow tick or repeated start_worker
    # call can't double-attach. Disable via SHADOW_CLOSE_CRON_ENABLED=false.
    try:
        from shared.runtime.shadow_close_cron import (
            start_worker as _start_shadow_close_cron,
        )
        _start_shadow_close_cron()
        logger.info("Shadow-close cron started (target 16:05 ET)")
    except Exception as e:  # noqa: BLE001
        logger.warning("shadow_close_cron start failed: %s", e)
    # Seed the initial patterns_universe watchlist (idempotent).
    # 2026-02-19: extended with `lane` field so the canonical
    # `symbol_in_universe` gate (shared/execution.py) can refuse
    # off-universe AND wrong-lane intents. Equity tickers tagged
    # `lane=equity`; the four Kraken-tracked majors are auto-seeded
    # with `lane=crypto` so Camaro/Chevelle/etc. have a canonical
    # crypto universe to propose against without an operator curl.
    try:
        from db import db as _db
        from namespaces import PATTERNS_UNIVERSE
        equity_seed = [
            "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "HOTH", "AMC", "GME",
        ]
        crypto_seed = [
            # Kraken-tracked majors (Phase 1)
            "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD",
            # Phase 2 expansion (2026-02-20) — added because Alpha
            # (crypto_strategist) was actively producing decision logs
            # on these pairs but the universe gate would reject any
            # routable intent. Operator confirmed Kraken has liquidity
            # on all four. Adding here makes the next deploy
            # automatically tradeable on these pairs.
            "AVAX/USD", "LINK/USD", "ADA/USD", "BNB/USD",
        ]
        for sym in equity_seed:
            await _db[PATTERNS_UNIVERSE].update_one(
                {"symbol": sym},
                {
                    "$setOnInsert": {
                        "symbol": sym,
                        "active": True,
                        "added_by": "seed",
                        "added_at": "seed",
                        "note": "Phase 1 seed",
                    },
                    # Idempotent: backfill `lane` onto any pre-existing
                    # rows without it. Legacy rows are equity.
                    "$set": {"lane": "equity"},
                },
                upsert=True,
            )
        for sym in crypto_seed:
            await _db[PATTERNS_UNIVERSE].update_one(
                {"symbol": sym},
                {
                    "$setOnInsert": {
                        "symbol": sym,
                        "active": True,
                        "added_by": "seed",
                        "added_at": "seed",
                        "note": "Crypto majors (auto-seed 2026-02-19)",
                    },
                    "$set": {"lane": "crypto"},
                },
                upsert=True,
            )
        logger.info(
            "patterns_universe seeded (%d equity + %d crypto)",
            len(equity_seed), len(crypto_seed),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("patterns_universe seed failed: %s", e)

    # ─── Boot-time legacy executor doc reconciliation ──────────────
    # 2026-02-20: companion to the auto-wipe-on-write helper in
    # `shared/roster.py`. Without this boot reconciliation, a deploy
    # that ships into prod with the legacy `shared_executor_seat`
    # doc already holding a stale value (e.g. 'alpha' from a
    # pre-QSS rotation) will keep the "SEAT REGISTRY DRIFT DETECTED"
    # banner firing on the Intents page until the operator does
    # SOMETHING that triggers a roster write.
    #
    # Doctrine: if the roster currently has an executor assignment,
    # the roster is authoritative — auto-clear the legacy doc on
    # boot so the diagnose surface is consistent without operator
    # intervention. If the roster's executor is null/None, leave
    # the legacy doc alone (legacy path still works as a fallback
    # for any caller that still uses /api/executor/rotate).
    try:
        from db import db as _db2
        from namespaces import BRAIN_ROSTER, SHARED_EXECUTOR_SEAT
        roster_doc = await _db2[BRAIN_ROSTER].find_one(
            {"_id": "current"},
            {"_id": 0, "assignments": 1},
        )
        roster_executor = ((roster_doc or {}).get("assignments") or {}).get("executor")
        legacy_doc = await _db2[SHARED_EXECUTOR_SEAT].find_one(
            {"_id": "executor"},
            {"_id": 0, "holder": 1},
        )
        legacy_holder = (legacy_doc or {}).get("holder")
        if roster_executor and legacy_holder and roster_executor != legacy_holder:
            await _db2[SHARED_EXECUTOR_SEAT].update_one(
                {"_id": "executor"},
                {"$set": {
                    "holder": None,
                    "since": None,
                    "assigned_by": "boot_reconcile",
                    "reason": (
                        f"auto-cleared at boot: roster.executor="
                        f"{roster_executor!r} but legacy doc held "
                        f"{legacy_holder!r}; roster is authoritative"
                    ),
                    "auto_cleared_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
            logger.info(
                "boot reconcile: cleared legacy shared_executor_seat "
                "(was %r, roster.executor=%r)",
                legacy_holder, roster_executor,
            )
        else:
            logger.info(
                "boot reconcile: legacy executor doc consistent with "
                "roster (roster=%r, legacy=%r) — no wipe needed",
                roster_executor, legacy_holder,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("legacy executor doc boot reconcile failed: %s", e)
    # Daily market snapshots — three S&P-500-wide point-in-time
    # captures per NYSE trading day (09:35 / 12:30 / 16:05 ET).
    # Doctrine: derived evidence only; never hits broker quotes.
    try:
        await ensure_daily_snapshot_indexes()
        start_daily_snapshot_worker()
        logger.info("daily_snapshot worker started")
    except Exception as e:  # noqa: BLE001
        logger.warning("daily_snapshot worker start failed: %s", e)
    # 2026-06-07 — Neutral brain stand-ins (Camino/Barracuda/Hellcat/GTO).
    # Stand-ins until the real per-brain wild_adaptive_core_v2 modules
    # are migrated to this stack. Gated by NEUTRAL_BRAINS_ENABLED
    # (default off); when on, 4 in-process asyncio tasks post intents
    # to MC over loopback so the fade class is structurally
    # impossible. The brains hold NO seat — operator-rotatable
    # seat policy still owns authority.
    try:
        import sys as _sys
        _sys.path.insert(0, "/app")
        from external.brains.runner import start_neutral_brains
        await start_neutral_brains()
    except Exception as e:  # noqa: BLE001
        logger.warning("neutral_brains start failed: %s", e)

    # Bracket outcome resolver — converts the brain's stated
    # `target_price`/`stop_price` thesis on every order into clean
    # categorical `tp_hit`/`sl_hit`/`timeout` labels for training.
    # Master-gated on RISEDUAL_BRACKET_OUTCOMES_ENABLED (default off);
    # when off the task is still spawned but just idles. Cheap.
    try:
        from shared.runtime.bracket_outcome_resolver import start_resolver_task
        start_resolver_task()
        logger.info("bracket_outcome_resolver task started")
    except Exception as e:  # noqa: BLE001
        logger.warning("bracket_outcome_resolver start failed: %s", e)
    yield
    await stop_poller()
    await stop_tickler()
    await stop_public_refresher()
    try:
        from shared.broker_fills import stop_broker_fills_poller
        await stop_broker_fills_poller()
    except Exception:  # noqa: BLE001
        pass
    await stop_auto_router()
    await stop_news_refresher()
    await stop_darkpool_refresher()
    await stop_scorecard_scheduler()
    await stop_position_monitor()
    await stop_paradox_coordinator()
    await stop_observation_resolver()
    try:
        from external.brains.runner import stop_neutral_brains
        await stop_neutral_brains()
    except Exception:  # noqa: BLE001
        pass
    # Paradox v2 background workers
    try:
        from shared.paradox_v2.verifier_loop import stop_verifier_loop
        from shared.paradox_v2.vote_session_sweeper import stop_vote_session_sweeper
        await stop_verifier_loop()
        await stop_vote_session_sweeper()
    except Exception:  # noqa: BLE001
        pass
    # Paradox v2 background workers
    try:
        from shared.paradox_v2.verifier_loop import stop_verifier_loop
        from shared.paradox_v2.vote_session_sweeper import stop_vote_session_sweeper
        await stop_verifier_loop()
        await stop_vote_session_sweeper()
    except Exception:  # noqa: BLE001
        pass
    try:
        await stop_daily_snapshot_worker()
    except Exception:  # noqa: BLE001
        pass
    try:
        from shared.opinion_resolver import stop_worker as _stop_opinion_resolver
        _stop_opinion_resolver()
    except Exception:  # noqa: BLE001
        pass
    try:
        await stop_finnhub_worker()
        await stop_polygon_worker()
        await stop_sec_edgar_worker()
        await stop_fred_worker()
    except Exception:  # noqa: BLE001
        pass
    try:
        from shared.runtime.opinion_silence_worker import (
            stop_worker as _stop_opinion_silence_worker,
        )
        await _stop_opinion_silence_worker()
    except Exception:  # noqa: BLE001
        pass
    # Graceful shutdown of the shadow-close cron — the lifespan
    # context exits here; cancelling the task lets the next
    # supervisor restart spin a fresh one.
    try:
        from shared.runtime.shadow_close_cron import (
            stop_worker as _stop_shadow_close_cron,
        )
        await _stop_shadow_close_cron()
    except Exception:  # noqa: BLE001
        pass
    client.close()


app = FastAPI(title="RISEDUAL Mission Control", lifespan=lifespan)

api_router = APIRouter(prefix="/api")


@api_router.get("/admin/neutral-brains/status")
async def neutral_brains_status():
    """Health-check the 4 in-process neutral brain runners.

    Returns the BRAIN_ROSTER (so the dashboard knows which slot maps
    to which car-name) and live stats (tick/intent/checkin counts).
    Empty `runners` list = NEUTRAL_BRAINS_ENABLED is false.

    Public read-only — no secrets exposed (tokens never returned).
    """
    try:
        import sys as _sys
        _sys.path.insert(0, "/app")
        from external.brains.runner import (
            BRAIN_ROSTER, is_enabled, runtime_stats,
        )
        return {
            "enabled": is_enabled(),
            "roster": [
                {"brain_id": b, "display_name": d, "token_env": t}
                for b, d, t in BRAIN_ROSTER
            ],
            "runners": runtime_stats(),
        }
    except Exception as e:  # noqa: BLE001
        return {"enabled": False, "error": str(e), "runners": []}


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
    # Doctrine (2026-05-18 rev): deploy_mode reports OBSERVABLE STATE
    # based on what the broker ADAPTERS can actually do, not on a
    # DB-side `execution_enabled` flag (which is decorative — the
    # adapters never read it). If a broker adapter can be constructed
    # from current credentials, that's live trading capability.
    env_mode = os.environ.get("DEPLOY_MODE", "observation").lower()
    derived_mode = "observation"
    if mongo_ok:
        try:
            # Crypto: a Kraken adapter loads iff valid credentials are
            # present + decrypt cleanly.
            from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
            kraken_adapter = await get_kraken_adapter()
            # Equity: Webull adapter loads iff env vars are armed.
            from shared.broker.webull import get_webull_adapter  # noqa: WPS433
            equity_adapter = await get_webull_adapter()
            if kraken_adapter is not None or equity_adapter is not None:
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
api_router.include_router(seat_nudges_router)
api_router.include_router(execution_router)
api_router.include_router(admin_wrappers_router)
api_router.include_router(admin_intents_post_mortem_router)
api_router.include_router(admin_auto_submit_router)
api_router.include_router(paradox_v2_router)
api_router.include_router(live_positions_router)
api_router.include_router(brain_lane_policy_router)
api_router.include_router(redeye_bridge_router)
api_router.include_router(risk_router)
api_router.include_router(vrl_router)
api_router.include_router(hypothesis_router)
api_router.include_router(mc_shelly_router)
api_router.include_router(patches_router)
api_router.include_router(runtime_tokens_router)
api_router.include_router(platform_survival_router)
api_router.include_router(sidecar_checkin_router)
api_router.include_router(confidence_floor_sweep_router)
api_router.include_router(snapshot_completeness_router)
api_router.include_router(memory_kernel_router)
api_router.include_router(orphan_inspection_router)
api_router.include_router(orphan_replay_router)
api_router.include_router(broker_freeze_router)
api_router.include_router(broker_reconcile_router)
api_router.include_router(sidecar_diagnostics_router)
api_router.include_router(data_stack_admin_router)
api_router.include_router(market_data_keys_router)
api_router.include_router(opinion_silence_watchdog_router)
api_router.include_router(heartbeat_reconciler_admin_router)
api_router.include_router(brain_outages_router)
api_router.include_router(brain_health_router)
api_router.include_router(market_data_snapshot_router)
api_router.include_router(daily_snapshots_router)
from routes.finnhub_backfill import router as finnhub_backfill_router
api_router.include_router(finnhub_backfill_router)
api_router.include_router(brain_runtime_router)
api_router.include_router(shelly_router)
api_router.include_router(brain_memory_ingest_router)
from routes.learning_scoreboard import router as learning_scoreboard_router
api_router.include_router(learning_scoreboard_router)
from routes.runtime_broker_status import router as runtime_broker_status_router
api_router.include_router(runtime_broker_status_router)
from routes.runtime_position_close import router as runtime_position_close_router
api_router.include_router(runtime_position_close_router)
from routes.runtime_cross_brain_memories import router as cross_brain_memories_router
api_router.include_router(cross_brain_memories_router)
api_router.include_router(paradox_router)
api_router.include_router(paradox_agent_router)
api_router.include_router(paradox_wake_router)
api_router.include_router(llm_ledger_router)
api_router.include_router(paradox_watchlist_router)
api_router.include_router(ai_run_router)
api_router.include_router(rise_ai_threads_router)
api_router.include_router(brain_emission_diagnose_router)
api_router.include_router(seat_registry_diagnose_router)
api_router.include_router(rise_ai_admin_router)
api_router.include_router(shelly_admin_extension_router)
api_router.include_router(sidecar_imposter_scan_router)
api_router.include_router(shelly_bus_router)
api_router.include_router(brain_doctrine_hint_router)
api_router.include_router(lane_execution_router)
api_router.include_router(observation_receipts_router)
api_router.include_router(learning_ladder_router)
api_router.include_router(auto_router_admin_router)
api_router.include_router(broker_fills_admin_router)
api_router.include_router(intent_summary_router)
api_router.include_router(mc_connection_stream_router)
api_router.include_router(position_misread_admin_router)
api_router.include_router(intent_inspect_router)

api_router.include_router(coordinator_router)
api_router.include_router(runtime_bundles_router)
api_router.include_router(promotion_artifact_report_router)
api_router.include_router(public_news_router)
api_router.include_router(public_darkpool_router)
api_router.include_router(diagnostics_router)
api_router.include_router(decisions_router)
api_router.include_router(doctrine_router)
api_router.include_router(doctrine_scorecard_router)
api_router.include_router(doctrine_auto_retire_router)
api_router.include_router(doctrine_promotion_router)
# Bracket outcomes — training-signal tile + read API.
from routes.admin_brackets import router as admin_brackets_router
api_router.include_router(admin_brackets_router)
api_router.include_router(quantum_router)
api_router.include_router(personalities_router)
api_router.include_router(flags_router)
api_router.include_router(alpha_router)
api_router.include_router(camaro_router)
api_router.include_router(chevelle_router)
api_router.include_router(storage_rollup_router)
api_router.include_router(trading_controls_router)
api_router.include_router(runtime_token_health_router)
api_router.include_router(alpha_vantage_admin_router)
api_router.include_router(broker_lane_admin_router)
api_router.include_router(intent_origin_router)
api_router.include_router(webull_admin_router)
api_router.include_router(parabolic_phase_admin_router)
api_router.include_router(data_council_admin_router)
api_router.include_router(broker_selection_router)
api_router.include_router(strategy_reference_router)
api_router.include_router(doctrine_training_router)
api_router.include_router(doctrine_eval_router)
api_router.include_router(outcome_join_admin_router)
api_router.include_router(shadow_outcome_admin_router)
api_router.include_router(scorecard_by_brain_router)
api_router.include_router(safety_gates_audit_router)

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

# CORS — explicit origin list from env (2026-05-26).
# Reads `CORS_ORIGINS` (the env var the operator already has set on
# prod). Comma-separated list. When set: exact-match origins +
# allow_credentials=True so cookie-based auth works. When unset:
# falls back to wildcard so preview / local dev keep working.
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
_cors_origins = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env and _cors_env != "*"
    else ["*"]
)
# Only enable credentialed CORS when origins are pinned — Starlette
# forbids `allow_credentials=True` alongside wildcard origins.
_cors_allow_credentials = _cors_origins != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
