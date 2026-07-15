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
import os
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
from shared.intent_sweeper import (
    start_sweeper_if_enabled as start_intent_sweeper,
    stop_sweeper as stop_intent_sweeper,
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
from shared.feeders.polygon_flatfiles import (
    start_worker_if_enabled as start_polygon_flatfiles_worker,
    stop_worker as stop_polygon_flatfiles_worker,
)
from shared.feeders.kraken_ohlc import (
    start_worker_if_enabled as start_kraken_ohlc_worker,
    stop_worker as stop_kraken_ohlc_worker,
)
from shared.feeders.webull_ohlc import (
    start_worker_if_enabled as start_webull_ohlc_worker,
    stop_worker as stop_webull_ohlc_worker,
)
from shared.external_signals.polygon_witness import (
    start_worker_if_enabled as start_polygon_news_witness,
    stop_worker as stop_polygon_news_witness,
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

    # Doctrine pin (2026-02-26 post-mortem): `ensure_indexes()` is
    # fire-and-forget at startup. Heavy compound-index builds on a
    # multi-million-row collection take minutes server-side; pymongo's
    # socket timeout (~15s) is much shorter. If we `await` here, the
    # client connection dies, the lifespan handler crashes, and the
    # pod never becomes Ready (broke prod deploy at 2026-06-30 00:28).
    # By scheduling on the event loop and returning immediately,
    # startup never blocks. The full index list — with per-index
    # status, longer timeouts, and a JSON response — is exposed at
    # `POST /api/admin/db/ensure-indexes` for operator-triggered
    # rebuilds that need observable outcomes.
    async def _ensure_indexes_soft() -> None:
        try:
            await ensure_indexes()
        except Exception:  # noqa: BLE001
            logger.exception(
                "ensure_indexes background task failed; app startup "
                "continues. Run POST /api/admin/db/ensure-indexes to "
                "retry with per-index status.",
            )
    try:
        asyncio.create_task(_ensure_indexes_soft())
    except Exception:  # noqa: BLE001
        logger.exception("ensure_indexes scheduling failed")
    await seed_admin(db)
    await seed_all(db)
    # 2026-07-01 Pass 2 delete: `shared/paradox_v2/`,
    # `shared/pipeline/receipts`, `shared/seat_state`,
    # `shared/brain_identity_migration` are all gone. The trader's
    # `seat_registry` is the single source of truth for seat state
    # now; no migration or trust-mirror is required.

    # System flags (2026-02-23) — DB-backed runtime feature toggles.
    # Boot the background refresher so the brain runner's sync reads
    # of `v3_brain_enabled()`, `is_watcher_enabled()`, and
    # `is_refire_enabled()` see DB state without an env-var edit +
    # restart. Operator can now flip from the dashboard.
    try:
        from shared.system_flags import start_background_refresher
        await start_background_refresher()
        logger.info("system_flags refresher started")
    except Exception as e:  # noqa: BLE001
        logger.warning("system_flags refresher failed to start (non-fatal): %s", e)
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

    # Webull broker credentials (2026-02-17) — hydrate in-process env
    # from the Mongo `webull_credentials` singleton if the operator
    # previously connected via POST /api/admin/webull/connect. Env
    # wins if already set (backward compat for pre-migration `.env`
    # deploys). Never blocks startup — Mongo unreachable is logged
    # and skipped.
    try:
        from shared.webull_credentials import hydrate_env_from_mongo
        hydrated = await hydrate_env_from_mongo(db)
        if hydrated:
            logger.info("webull creds hydrated from mongo singleton into process env")
    except Exception as e:  # noqa: BLE001
        logger.warning("webull credential hydration failed (non-fatal): %s", e)

    # Kraken pair-floor auto-seeder (2026-02-17) — background task
    # that periodically fetches Kraken's AssetPairs + Ticker, computes
    # `ordermin × mid` per pair, and upserts the result as a default
    # floor. Operator-set rows are NEVER overwritten. Startup is
    # non-blocking; the task swallows its own errors and never
    # crashes the server.
    try:
        from shared.kraken_auto_seed import start_background_task
        app.state.kraken_auto_seed_task = await start_background_task()
        logger.info("kraken_auto_seed background task started")
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_auto_seed task failed to start (non-fatal): %s", e)


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

    # ── Stale-intent sweeper (2026-02-19 operator directive) ──────
    # 30-minute cadence, 6-hour age gate, archive-then-delete
    # (learning-aware bifurcation). Scheduler ON by default.
    # Flip `INTENT_SWEEPER_ENABLED=false` in backend/.env to pause.
    try:
        start_intent_sweeper(db)
    except Exception as e:  # noqa: BLE001
        logger.error("intent_sweeper start failed (non-fatal): %s", e)

    # ── 2026-02-19 sidecar trader — DECOMMISSIONED ────────────────
    # The standalone sidecar loop was demoted to shadow mode in
    # iter-22 (TRADER_ENABLED=false) and formally deleted in iter-23.
    # `/app/trader/` now hosts only the MC-support layer
    # (webull_auth / spread / spread_stream / store / state /
    # merge_rights / config) — no orchestration remains. MC's
    # `shared/auto_router.py` is the sole broker authority.
    #
    # What we still initialise here is the MC-support surface the
    # dashboard reads:
    #   * trader.store  — SQLite truth tape (`/api/admin/trader/*`)
    #   * trader.state  — hydrated in-memory state (dashboard reads)
    #   * trader.spread — spread poller (dashboard tile)
    #   * trader.spread_stream — Webull v2 live-quote stream
    import os as _os
    import sys as _sys
    if "/app" not in _sys.path:
        _sys.path.insert(0, "/app")

    try:
        from trader import config as _trader_config  # noqa: WPS433
        from trader import store as _trader_store    # noqa: WPS433
        from trader import state as _trader_state    # noqa: WPS433
        _trader_store.init(
            _trader_config.sqlite_path(),
            _trader_config.jsonl_dir(),
        )
        _trader_state.hydrate_from_sqlite()
        logger.info(
            "trader.store initialized (MC dashboard support layer)",
        )
    except Exception as e:  # noqa: BLE001
        logger.error("trader.store init failed (non-fatal): %s", e)

    # Spread poller — dashboard-only telemetry (no broker calls).
    try:
        import asyncio as _asyncio_sp
        from trader import spread as _trader_spread  # noqa: WPS433
        app.state.spread_task = _asyncio_sp.create_task(
            _trader_spread.poll_loop(),
            name="mc.trader.spread.poll",
        )
        logger.info("trader.spread poller STARTED (dashboard-only)")
    except Exception as e:  # noqa: BLE001
        logger.warning("trader.spread poll start failed (non-fatal): %s", e)

    # Webull v2 MQTT quote stream — dashboard tile. Opt-in via
    # TRADER_EQUITY_STREAM_ENABLED=true.
    try:
        from trader import spread_stream as _trader_stream  # noqa: WPS433
        _trader_stream.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("trader.spread_stream start failed (non-fatal): %s", e)

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
    # RISE AI learning loop (2026-02-25) — auto-grader + JSONL harvester.
    # Promotes the previously one-shot scaffolding into a live worker so
    # the SHADOW corpus actually grows from Claude traffic. Kill switch:
    # RISE_LEARNING_LOOP_ENABLED=false in backend/.env.
    try:
        from shared.rise_ai.learning_loop import start_rise_learning_loop  # noqa: WPS433
        start_rise_learning_loop()
        logger.info("RISE AI learning loop started")
    except Exception as e:  # noqa: BLE001
        logger.warning("rise_ai learning loop start failed: %s", e)
    # Seed default brain × lane emission policy (idempotent).
    try:
        await seed_default_policy()
        logger.info("Brain × lane emission policy seeded")
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_lane_policy seed failed: %s", e)
    # Seed brain_legend collection (2026-02-23 dual-field migration).
    # Idempotent. Provides operator-visible legacy→canonical mapping
    # surfaced at `/api/admin/brain-legend`.
    try:
        from shared.brain_legend import seed_brain_legend  # noqa: WPS433
        summary = await seed_brain_legend(db)
        logger.info("brain_legend seeded: %s", summary)
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_legend seed failed: %s", e)
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
        start_polygon_flatfiles_worker()
        start_polygon_news_witness()
        start_sec_edgar_worker()
        start_fred_worker()
        start_quiver_worker()
        # 2026-02-20: crypto RVOL 20-day baseline via Kraken daily
        # OHLC. Public endpoint, no auth. Idle no-op if disabled.
        start_kraken_ohlc_worker()
        # 2026-07 iter-27: Webull as PRIMARY equity intraday feeder
        # (broker-native — what you can trade against). Finnhub &
        # polygon_flatfiles remain as backup/daily via source
        # tagging + ORDER BY ts DESC in consumers.
        start_webull_ohlc_worker()
    except Exception as e:  # noqa: BLE001
        logger.warning("data_stack workers start failed: %s", e)
    # Per-Lane Capital Cap Ledger — atomic reservation store
    # (2026-02-20). Idempotent init: creates the equity/crypto
    # ledger docs if absent, refreshes `total` from env caps on
    # every boot without touching live `reserved` state.
    try:
        from shared.capital.ledger import init_ledger as _init_capital_ledger
        equity_cap = float(
            os.environ.get("EQUITY_CAPITAL_CAP_USD") or 1000.0,
        )
        crypto_cap = float(
            os.environ.get("CRYPTO_CAPITAL_CAP_USD") or 500.0,
        )
        await _init_capital_ledger(equity_cap, crypto_cap)
    except Exception as e:  # noqa: BLE001
        logger.warning("capital_ledger init failed: %s", e)
    # Capital ledger stale-reservation sweeper (2026-02-20 P1 wire-up).
    # Frees any `open` reservations older than the lane-specific
    # threshold — cleans up crash-artifact reservations that no live
    # order will ever reconcile.
    try:
        from shared.capital.sweeper import (
            start_worker_if_enabled as _start_capital_ledger_sweeper,
        )
        _start_capital_ledger_sweeper()
    except Exception as e:  # noqa: BLE001
        logger.warning("capital_ledger_sweeper start failed: %s", e)
    # Distribution Snapshot Job (2026-02-20). Persists per-(brain, lane,
    # window) behavioral fingerprints every 15 min. Enables before/after
    # doctrine change validation.
    try:
        from shared.session_fingerprint import (
            start_worker_if_enabled as _start_session_fingerprint,
        )
        _start_session_fingerprint()
    except Exception as e:  # noqa: BLE001
        logger.warning("session_fingerprint start failed: %s", e)

    # 2026-02-23 — Native brain runtimes (in-process brains).
    # Consolidates the previously-external sidecars into MC. Each
    # brain is flag-gated by `<BRAIN>_NATIVE_RUNTIME_ENABLED` (default
    # false) so deploying this code does NOT flip behavior until the
    # operator explicitly turns each one on. Doctrine: brains think
    # separately, MC schedules them together, only canonical pipeline
    # emits, only seat holder can execute.
    _BRAIN_IDS = ("barracuda", "gto", "camino", "hellcat")

    # 2026-02-19 (operator directive): boot-time strategy-collision
    # guard. Each brain's `strategy.py` MUST hash to a distinct
    # value; if two ever converge that's a real bug (bad refactor,
    # accidental symlink) and should fail loud rather than silently
    # let two "different" brains run identical decision code.
    try:
        from shared.brains._strategy_identity import assert_no_strategy_collisions
        assert_no_strategy_collisions(_BRAIN_IDS)
    except AssertionError as e:
        # Log LOUD but do NOT block boot — we want the app up to
        # investigate; the `strategy_sha` field on the status
        # payload will make the collision visible operationally.
        logger.error("STRATEGY_SHA_COLLISION at boot: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("strategy_sha boot check skipped: %s", e)

    for _brain_name in _BRAIN_IDS:
        try:
            import importlib
            _mod = importlib.import_module(
                f"shared.runtime.{_brain_name}_runtime"
            )
            _mod.start_worker()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "%s_native_runtime start failed: %s", _brain_name, e,
            )

    # 2026-02-23 — Advisor/consensus collection — TTL + lookup indexes.
    # Idempotent; safe at every boot. Storage is no-op until
    # CONSENSUS_MODE_ENABLED=true flips on per-intent writes.
    try:
        from db import db as _db
        from shared.advisor_opinions import ensure_indexes as _ensure_advisor_indexes
        await _ensure_advisor_indexes(_db)
    except Exception as e:  # noqa: BLE001
        logger.warning("advisor_opinions index ensure failed: %s", e)
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
    # 2026-06-07 → 2026-07-12: Neutral brain stand-ins retired.
    # The 4 pulse brains (`mc_brains/`) now do this work directly
    # via the pulse loop registered above. `external/brains/runner.py`
    # was deleted in P3 step 3 after 100% gates-pass on all 4 brains.
    # The kill switch `RISEDUAL_LEGACY_RUNNERS_ENABLED` was retired
    # simultaneously — there are no runners to re-enable.

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

    # ── MC Pulse (2026-07-11, iter-27) — migration step 3 ──
    # Registers pilot brain (Camino) + starts a 15s pulse loop in
    # `compare_only=True` mode. Nothing routes to the arbiter tape
    # or the trader from this path — envelopes go to
    # `mc_opinions_compare` so we can measure parity vs the
    # existing Camino runner without disturbing live behavior.
    #
    # Gated on `RISEDUAL_MC_PULSE_ENABLED=1` so the default is OFF
    # in preview environments until the operator flips it.
    #
    # See:
    #   /app/memory/MC_PULSE.md            (design freeze)
    #   /app/memory/CAMINO_RUNNER_AUDIT.md (implicit-contract audit)
    if os.environ.get("RISEDUAL_MC_PULSE_ENABLED", "0") == "1":
        try:
            from mc_brains.camino import CaminoBrain
            from mc_brains.gto import GtoBrain
            from mc_brains.barracuda import BarracudaBrain
            from mc_brains.hellcat import HellcatBrain
            from mc_pulse.registry import get_registry
            from mc_pulse.pulse_worker import start_pulse_worker

            registry = get_registry()
            # 2026-07-12 P2 step 2: register all 4 pulse brains.
            # Each is an independent Brain instance; a broken brain
            # is contained by `mc_pulse.containment.evaluate_brain`
            # and does NOT silence the other 3.
            for brain_cls in (CaminoBrain, GtoBrain, BarracudaBrain, HellcatBrain):
                inst = brain_cls()
                if inst.id not in registry.ids():
                    registry.register(inst)
            start_pulse_worker(app)
            logger.info(
                "mc_pulse worker started (compare_only=True, cadence=15s, brains=%s)",
                registry.ids(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("mc_pulse start failed: %s", e)
    else:
        logger.info("mc_pulse disabled (set RISEDUAL_MC_PULSE_ENABLED=1 to arm the migration pulse)")

    # ── 2026-07-12 (P4) doctrine: pulse-health trend snapshotter ────
    # Rolling snapshot of the four brains' pulse-health metrics
    # (evaluation_count, action_distribution, confidence_mean/std,
    # stale_input_rate, no_data_rate, exception_rate, distinctness,
    # pulse_lag_ms, duplicate_opinion_rate, latest_source_bar_at).
    # Persists compact rows to `mc_pulse_health_snapshots` so the
    # operator dashboard can plot cognitive-health trends without
    # stalking `/api/mc/pulse-health`. Replaces the earlier
    # parity snapshotter which measured runner-vs-pulse metrics
    # that stopped being meaningful when runners were deleted (P3).
    # ONLY runs when the pulse itself is armed.
    if os.environ.get("RISEDUAL_MC_PULSE_ENABLED", "0") == "1":
        try:
            interval_min = int(
                os.environ.get("PULSE_HEALTH_SNAPSHOT_INTERVAL_MIN", "15")
            )
            window_hours = int(
                os.environ.get("PULSE_HEALTH_SNAPSHOT_WINDOW_HOURS", "24")
            )

            async def _pulse_health_snapshot_loop():
                from mc_pulse.pulse_health_routes import (  # noqa: WPS433
                    PULSE_HEALTH_SNAPSHOT_BRAINS,
                    take_pulse_health_snapshot,
                )
                # Small startup delay so pulse has time to accumulate
                # a first batch before the first snapshot fires.
                await asyncio.sleep(60.0)
                while True:
                    for brain in PULSE_HEALTH_SNAPSHOT_BRAINS:
                        await take_pulse_health_snapshot(
                            brain, hours=window_hours,
                        )
                    await asyncio.sleep(interval_min * 60.0)

            app.state.pulse_health_snapshot_task = asyncio.create_task(
                _pulse_health_snapshot_loop(),
            )
            logger.info(
                "pulse_health_snapshotter started interval_min=%d "
                "window_hours=%d brains=%s",
                interval_min, window_hours,
                ["camino", "gto", "barracuda", "hellcat"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("pulse_health_snapshotter start failed: %s", e)

    # ── Universe refresher (2026-07-15, iter-30 P4) ──────────────
    # Rebuilds `live_universe` per lane every 15min from broker
    # screeners (Webull for equity, Kraken for crypto). Fail-soft:
    # a broken start MUST NOT keep the pulse from running — the
    # snapshot_service falls back through `patterns_universe` →
    # env defaults automatically.
    try:
        from shared.universe.refresher import universe_refresher_loop
        app.state.universe_refresher_task = asyncio.create_task(
            universe_refresher_loop(),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("universe_refresher start failed: %s", e)

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

    # MC Pulse shutdown — cancel the background loop cleanly.
    try:
        from mc_pulse.pulse_worker import stop_pulse_worker
        await stop_pulse_worker(app)
    except Exception:  # noqa: BLE001
        pass

    # Pulse-health snapshotter shutdown (2026-07-12 P4).
    try:
        t = getattr(app.state, "pulse_health_snapshot_task", None)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    # Stale-intent sweeper shutdown (2026-02-19).
    try:
        await stop_intent_sweeper()
    except Exception:  # noqa: BLE001
        pass

    # ── 2026-02-19 sidecar trader shutdown — DECOMMISSIONED ───────
    # The `app.state.trader_task` no longer exists (sidecar loop
    # deleted). Nothing to stop here. The spread poller + stream
    # shutdown below still applies — those are the dashboard-only
    # telemetry loops.

    # ── 2026-07-02 spread poller shutdown (dashboard-only) ────────
    try:
        st = getattr(app.state, "spread_task", None)
        if st and not st.done():
            st.cancel()
            try:
                import asyncio as _asyncio_sp2
                await _asyncio_sp2.wait_for(st, timeout=10)
            except Exception:  # noqa: BLE001
                pass
            logger.info("trader.spread poller stopped.")
    except Exception:  # noqa: BLE001
        pass

    # ── 2026-07-02 spread MQTT stream shutdown ────────────────────
    try:
        from trader import spread_stream as _trader_stream_sd  # noqa: WPS433
        _trader_stream_sd.stop(timeout=5.0)
        logger.info("trader.spread_stream stopped.")
    except Exception:  # noqa: BLE001
        pass

    await stop_news_refresher()
    await stop_darkpool_refresher()
    await stop_scorecard_scheduler()
    # RISE AI learning loop — graceful cancel.
    try:
        from shared.rise_ai.learning_loop import stop_rise_learning_loop  # noqa: WPS433
        await stop_rise_learning_loop()
    except Exception:  # noqa: BLE001
        pass
    await stop_position_monitor()
    await stop_paradox_coordinator()
    await stop_observation_resolver()
    # 2026-07-12 (P3 step 3): `stop_neutral_brains` used to live in
    # `external.brains.runner`; that module was deleted after the
    # kill switch + observation window confirmed pulse-only stability.
    # The pulse worker's shutdown handler (below) is the sole
    # brain-stop path now.
    # Paradox v2 background workers — REMOVED in 2026-07-01 Pass 2 delete.
    # (previously stopped verifier_loop + vote_session_sweeper)
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
        await stop_polygon_flatfiles_worker()
        await stop_polygon_news_witness()
        await stop_sec_edgar_worker()
        await stop_fred_worker()
        await stop_quiver_worker()
        await stop_kraken_ohlc_worker()
        await stop_webull_ohlc_worker()
    except Exception:  # noqa: BLE001
        pass
    try:
        from shared.capital.sweeper import stop_worker as _stop_cap_sweeper
        await _stop_cap_sweeper()
    except Exception:  # noqa: BLE001
        pass
    try:
        from shared.session_fingerprint import stop_worker as _stop_fp
        await _stop_fp()
    except Exception:  # noqa: BLE001
        pass
    try:
        from shared.runtime.barracuda_runtime import (
            stop_worker as _stop_barracuda_runtime,
        )
        await _stop_barracuda_runtime()
    except Exception:  # noqa: BLE001
        pass
    for _brain_name in ("gto", "camino", "hellcat"):
        try:
            import importlib
            _mod = importlib.import_module(
                f"shared.runtime.{_brain_name}_runtime"
            )
            await _mod.stop_worker()
        except Exception:  # noqa: BLE001
            pass
    try:
        from shared.brain_tuning_cache import stop_refresher as _stop_brain_tuning_refresher
        await _stop_brain_tuning_refresher()
    except Exception:  # noqa: BLE001
        pass
    client.close()
