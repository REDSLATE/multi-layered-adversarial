"""FastAPI lifespan — boot migrations, worker start/stop, graceful shutdown.

Extracted verbatim from the original `server.py` lifespan function on
2026-06-18. Behavior is 1:1 with the pre-refactor code; only the
import scope changed (everything the lifespan needs is now imported
locally here instead of at the server.py module level).

The lifespan owns three concerns:
    1. Boot-time migrations and seeds (brain_identity, paradox_v2,
       seat_state, patterns_universe, legacy executor doc reconciliation).
    2. Cache hydration for operator-flippable Mongo overrides
       (unified pipeline flag, webull floor, exposure caps, auto-submit).
    3. Background-worker start (auto-router, position monitor, daily
       snapshots, data feeders, neutral brains, watchdogs, cron jobs)
       and graceful shutdown.

Do not reorder phases without re-reading the inline comments — several
migrations have explicit doctrine-pinned ordering (e.g. brain_identity
rename MUST run before seat_state migrations).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from db import db, client
from db import ensure_indexes
from auth import seed_admin
from shared.crypto.routes import start_poller_if_needed, stop_poller
from shared.ibkr import start_tickler_if_needed, stop_tickler
from shared.public import (
    start_refresher_if_needed as start_public_refresher,
    stop_refresher as stop_public_refresher,
)
from shared.public_api.rate_limit import (
    ensure_ttl_index as _rate_limit_ensure_ttl,
)
from shared.public_api.news import (
    start_news_refresher,
    stop_news_refresher,
)
from shared.public_api.dark_pool import (
    start_darkpool_refresher,
    stop_darkpool_refresher,
)
from shared.brain_lane_policy import seed_default_policy
from shared.risk.position_monitor import (
    start_monitor_if_enabled as start_position_monitor,
    stop_monitor as stop_position_monitor,
)
from shared.vrl import start_scorecard_scheduler, stop_scorecard_scheduler
from shared.auto_router import (
    start_auto_router_if_enabled,
    stop_auto_router,
)
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
from shared.alt_data.quiver_quant import (
    start_worker_if_enabled as start_quiver_worker,
    stop_worker as stop_quiver_worker,
)
from shared.flags import get_flags_snapshot
from shared.seed import seed_all
from shared.coordinator.lifespan import (
    start_paradox_coordinator,
    stop_paradox_coordinator,
)
from shared.coordinator.user_seed import ensure_coordinator_user
from shared.observation_resolver import (
    start_observation_resolver,
    stop_observation_resolver,
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

    # Unified pipeline (2026-02-20) — idempotent index ensurer for
    # `pipeline_receipts`. Safe no-op when collection already has the
    # indexes. Required for the /api/intents/{id}/why endpoint.
    try:
        from shared.pipeline.receipts import ensure_indexes as ensure_pipeline_indexes
        await ensure_pipeline_indexes()
        logger.info("pipeline_receipts indexes ensured")
    except Exception as e:  # noqa: BLE001
        logger.warning("pipeline_receipts index ensure failed (non-fatal): %s", e)

    # Seat-state single-source-of-truth migration (2026-02-20). Copies
    # the legacy `shared_auditor_seat.holder` into the canonical
    # `brain_roster.assignments.auditor` if the roster slot is empty.
    # Idempotent: re-running on a healed roster is a no-op.
    try:
        from shared.seat_state import (
            migrate_legacy_auditor_to_roster,
            sync_v2_trust_from_roster,
        )
        # Run the canonical-rename migration FIRST so the auditor
        # migration + v2 trust sync operate on the new names.
        from shared.brain_identity_migration import migrate_brain_identity
        rename_report = await migrate_brain_identity()
        # Only log if anything actually changed — keeps boot logs quiet
        # once the migration has settled.
        actual = {k: v for k, v in (rename_report.get("updates") or {}).items() if v}
        if actual:
            logger.info("brain_identity rename migration: %s", actual)
        result = await migrate_legacy_auditor_to_roster()
        logger.info("seat_state migration: %s", result)
        sync_result = await sync_v2_trust_from_roster()
        logger.info("seat_state v2 trust sync: %s", sync_result)
    except Exception as e:  # noqa: BLE001
        logger.warning("seat_state migration failed (non-fatal): %s", e)

    # Unified pipeline flag — REMOVED 2026-06-18. The pipeline is now
    # unconditional (legacy 20-gate chain deleted). Operator kill
    # switches are `/api/admin/auto-router/stop` (full loop halt) and
    # `/api/admin/trading/disable` (per-order RoadGuard hard stop).

    # Webull min-notional floor override — same pattern. 2026-02-21:
    # operator declared "Webull min is $1" but Prod env var stayed at
    # $3, so blocking 27+ intents/day with WEBULL_NOTIONAL_BELOW_FLOOR.
    # The Mongo flag wins over env so the operator can drop the floor
    # to $1 from the admin UI without a redeploy.
    try:
        from shared.broker.webull_caps import refresh_webull_floor_cache
        wf = await refresh_webull_floor_cache()
        logger.info("webull_min_notional_floor override (from mongo) = %s", wf)
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_min_notional_floor refresh failed (non-fatal): %s", e)

    # Exposure caps override — same pattern. 2026-06-18 (live pilot):
    # Prod hit cap_per_day=$50 two hours before market open with no
    # way to flip the env var from a phone. Mongo override lets the
    # operator raise/lower per_order/per_day/open_notional caps from
    # the admin UI without a redeploy.
    try:
        from shared.exposure_caps import refresh_cap_overrides_cache
        co = await refresh_cap_overrides_cache()
        logger.info(
            "exposure_caps_override (from mongo) = enabled=%s per_day=%s",
            co.get("enabled"), co.get("per_day_usd"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("exposure_caps refresh failed (non-fatal): %s", e)

    # Brain-tuning override cache (2026-06-19). Background refresher
    # pulls the runtime_flags.brain_tuning override every 30s so
    # brain_core sees operator threshold flips within one tick.
    try:
        from shared.brain_tuning_cache import (
            refresh_cache as _refresh_brain_tuning,
            start_refresher_if_needed as _start_brain_tuning_refresher,
        )
        await _refresh_brain_tuning()
        _start_brain_tuning_refresher()
        logger.info("brain_tuning cache refresher started (TTL=30s)")
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_tuning cache start failed (non-fatal): %s", e)


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
        start_quiver_worker()
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
    # doc already holding a stale value (e.g. 'camino' from a
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
        await stop_quiver_worker()
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
    try:
        from shared.brain_tuning_cache import stop_refresher as _stop_brain_tuning_refresher
        await _stop_brain_tuning_refresher()
    except Exception:  # noqa: BLE001
        pass
    client.close()
