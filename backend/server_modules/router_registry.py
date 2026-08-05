"""Central registration of all API routers — manifest + discovery.

Refactored 2026-07-31 (was ~150 import lines + ~150 include calls).
Behavior is 1:1 with the previous explicit version — proven by the
route-table snapshot tripwire (tests/test_router_registry.py against
tests/fixtures/route_table_snapshot.json).

ORDER MATTERS: FastAPI is first-match-wins on overlapping paths, so
`ROUTER_SPECS` order defines which router serves a conflicting route.
The manifest preserves the original server.py order exactly — do not
reorder without understanding the consequences.

Adding a new router:
  1. Create the module with a module-level `router = APIRouter(...)`.
  2. Add ONE line to `ROUTER_SPECS` ("pkg.module:attr") — position it
     deliberately if its paths can overlap an existing router.
  3. Regenerate the snapshot fixture (command in the tripwire test).

Safety net: any `routes/` module exposing an `APIRouter` named
`router` that is NOT listed here (and not in `SKIP_DISCOVERY`) is
auto-registered at the END with a WARNING — a forgotten wiring line
degrades to a log complaint instead of silent 404s. The tripwire
keeps this path empty in CI.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from fastapi import APIRouter

logger = logging.getLogger("risedual.router_registry")

# "module.path:attr" — attr is an APIRouter, or a list/tuple of them
# (e.g. the intent-bridge factories). Order preserved from the
# pre-refactor explicit include calls.
ROUTER_SPECS: tuple[str, ...] = (
    "auth:router",
    "shared.routes:router",
    "shared.ingest:router",
    "shared.opinions:router",
    "shared.outcomes:router",
    "shared.conflicts:router",
    "shared.positions:router",
    "shared.public_api:router",
    "shared.public_api.traffic:router",
    "shared.seat_performance:router",
    "shared.technicals:router",
    "shared.crypto.routes:router",
    "shared.ibkr:router",
    "shared.public:router",
    "shared.roster:router",
    "shared.doctrine:router",
    "shared.intents:router",
    "mc_arbiter.routes:router",
    "mc_pulse.parity_routes:router",
    "mc_pulse.pulse_health_routes:router",
    "shared.executor_seat:router",
    "shared.auditor_seat:router",
    "shared.seat_nudges:router",
    "routes.admin_hot_brain_router:router",
    "routes.admin_spread_quality:router",
    "routes.webull_credentials:router",
    "routes.intent_clearance_funnel:router",
    "routes.seats_reverse_sync:router",
    "routes.kraken_pair_floors:router",
    "routes.admin_system_flags:router",
    "routes.admin_brain_legend:router",
    "routes.admin_execution_lifecycle_funnel:router",
    "routes.admin_brain_input_health:router",
    "routes.admin_external_signals:router",
    "routes.admin_feature_coverage:router",
    "routes.admin_capital_ledger:router",
    "routes.admin_session_fingerprint:router",
    "routes.webull_caps_admin:router",
    "routes.exposure_caps_admin:router",
    "routes.equity_extended_hours_admin:router",
    "routes.brain_tuning_admin:router",
    "routes.pipeline_blocker_histogram:router",
    "routes.server_time_admin:router",
    "routes.db_admin:router",
    "routes.healthcheck_full:router",
    "routes.pipeline_doctor:router",
    "routes.kill_map:router",
    "routes.intent_trace:router",
    "routes.admin_quiver:router",
    "shared.live_positions:router",
    "shared.brain_lane_policy:router",
    "shared.redeye_crypto_intent_bridge:router",
    "shared.chevelle_crypto_intent_bridge:router",
    "shared.equity_intent_bridges:EQUITY_ROUTERS",
    "shared.crypto_intent_bridges:CRYPTO_ROUTERS",
    "shared.risk.routes:router",
    "shared.vrl:router",
    "shared.mc_shelly:router",
    "shared.patches:router",
    "shared.runtime.routes:router",
    "shared.runtime.sidecar_checkin:router",
    "shared.calibration.confidence_floor_sweep:router",
    "shared.calibration.snapshot_completeness:router",
    "routes.memory_kernel_routes:router",
    "routes.broker_freeze_routes:router",
    "routes.broker_reconcile_routes:router",
    "routes.data_stack_admin:router",
    "routes.market_data_keys:router",
    "routes.brain_outages:router",
    "routes.market_data_snapshot:router",
    "routes.daily_snapshots:router",
    "routes.finnhub_backfill:router",
    "routes.brain_runtime:router",
    "routes.brain_memory_ingest:router",
    "routes.runtime_broker_status:router",
    "routes.runtime_position_close:router",
    "routes.runtime_cross_brain_memories:router",
    "routes.llm_ledger_routes:router",
    "routes.ai_run_routes:router",
    "routes.rise_ai_threads_routes:router",
    "routes.brain_emission_diagnose:router",
    "routes.seat_registry_diagnose:router",
    "routes.rise_ai_admin:router",
    "routes.brain_doctrine_hint:router",
    "shared.lane_execution:router",
    "shared.observation_receipts:router",
    "shared.learning_ladder:router",
    "routes.auto_router_admin:router",
    "routes.retention_admin:router",
    "routes.gate_failure_digest:router",
    "routes.kraken_pair_admin:router",
    "routes.universe_admin:router",
    "routes.momentum_scanner_routes:router",
    "routes.missed_entries_admin:router",
    "routes.operator_alerts:router",
    "routes.tape_quality_admin:router",
    "routes.sell_point_admin:router",
    "routes.exit_admin:router",
    "routes.expectancy_admin:router",
    "routes.hotpath_admin:router",
    "routes.gain_goal_admin:router",
    "routes.risk_sizer_admin:router",
    "routes.options_admin:router",
    "routes.pipeline_admin:router",
    "routes.scanner_admin:router",
    "routes.risk_budget_admin:router",
    "routes.opportunity_admin:router",
    "routes.kraken_universe_admin:router",
    "routes.intent_sweeper_admin:router",
    "routes.counterfactuals_admin:router",
    "routes.broker_fills_admin:router",
    "routes.intent_summary:router",
    "routes.mc_connection_stream:router",
    "routes.position_misread_admin:router",
    "shared.coordinator.routes:router",
    "shared.runtime_bundles:router",
    "shared.public_api.news:router",
    "shared.public_api.dark_pool:router",
    "shared.diagnostics:router",
    "shared.doctrine_routes:router",
    "shared.doctrine:scorecard_router",
    "shared.doctrine:auto_retire_router",
    "routes.admin_brackets:router",
    "routes.trader_stats:router",
    "shared.quantum_routes:router",
    "shared.personalities_routes:router",
    "shared.flags:router",
    "runtimes.alpha.routes:router",
    "runtimes.camaro.routes:router",
    "runtimes.chevelle.routes:router",
    "routes.storage_rollup:router",
    "routes.trading_controls:router",
    "routes.admin_learning:router",
    "routes.alpha_vantage_admin:router",
    "routes.broker_lane_admin:router",
    "routes.symbol_registry_admin:router",
    "routes.live_universe_admin:router",
    "routes.intent_origin:router",
    "routes.webull_admin:router",
    "routes.broker_selection:router",
    "routes.strategy_reference:router",
    "routes.outcome_join_admin:router",
    "routes.safety_gates_audit:router",
    "routes.intents_purge_admin:router",
)

# `routes/` modules intentionally NOT auto-registered. Add a module
# name here (without the `routes.` prefix) to park it unwired.
SKIP_DISCOVERY: frozenset[str] = frozenset()


def _resolve(spec: str):
    mod_path, attr = spec.split(":")
    return getattr(importlib.import_module(mod_path), attr)


def register_routers(api_router: APIRouter) -> None:
    """Attach every sub-router to the parent `api_router` in
    manifest order, then sweep `routes/` for unlisted modules."""
    for spec in ROUTER_SPECS:
        obj = _resolve(spec)
        for r in (obj if isinstance(obj, (list, tuple)) else (obj,)):
            api_router.include_router(r)

    # Convention discovery — appended at the END so a forgotten
    # manifest line can never shadow an existing route.
    listed = {s.split(":", 1)[0] for s in ROUTER_SPECS}
    import routes as routes_pkg  # noqa: WPS433
    for info in sorted(pkgutil.iter_modules(routes_pkg.__path__), key=lambda i: i.name):
        mod_path = f"routes.{info.name}"
        if mod_path in listed or info.name in SKIP_DISCOVERY:
            continue
        r = getattr(importlib.import_module(mod_path), "router", None)
        if isinstance(r, APIRouter):
            logger.warning(
                "router_registry: auto-discovered unlisted %s — appended "
                "at end; add it to ROUTER_SPECS to pin its order",
                mod_path,
            )
            api_router.include_router(r)
