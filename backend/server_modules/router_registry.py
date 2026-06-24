"""Central registration of all API routers.

Extracted from `server.py` on 2026-06-18. Behavior is 1:1: every router
that was previously included via `api_router.include_router(...)` at
module load time is now included by `register_routers(api_router)`,
called once during app construction.

Call order matches the original server.py exactly — do not reorder
without understanding the consequences. FastAPI's first-match wins
on path conflicts, so the order here defines which router serves a
given route when two routers register overlapping prefixes (the
original file relied on the order documented below).
"""
from __future__ import annotations

from fastapi import APIRouter

from auth import router as auth_router
from shared.routes import router as shared_router
from shared.ingest import router as ingest_router
from shared.opinions import router as opinions_router
from shared.outcomes import router as outcomes_router
from shared.conflicts import router as conflicts_router
from shared.technicals import router as technicals_router
from shared.crypto.routes import router as kraken_router
from shared.ibkr import router as ibkr_router
from shared.public import router as public_router
from shared.positions import router as positions_router
from shared.sovereign_mode_guard import router as sovereign_router
from shared.public_api import router as public_api_router
from shared.public_api.traffic import router as public_traffic_router
from shared.seat_performance import router as seat_performance_router
from shared.roster import router as roster_router
from shared.promotion import router as promotion_router
from shared.diagnostics import router as diagnostics_router
from shared.doctrine import (
    router as doctrine_legacy_router,
    scorecard_router as doctrine_scorecard_router,
    auto_retire_router as doctrine_auto_retire_router,
    promotion_router as doctrine_promotion_router,
)
from shared.flags import router as flags_router
from shared.intents import router as intents_router
from shared.executor_seat import router as executor_router
from shared.auditor_seat import router as auditor_router
from shared.seat_nudges import router as seat_nudges_router
from shared.decisions_feed import router as decisions_router
from shared.doctrine_routes import router as doctrine_router
from shared.execution import router as execution_router
from shared.live_positions import router as live_positions_router
from shared.brain_lane_policy import router as brain_lane_policy_router
from shared.redeye_crypto_intent_bridge import router as redeye_bridge_router
from shared.chevelle_crypto_intent_bridge import router as chevelle_bridge_router
from shared.equity_intent_bridges import EQUITY_ROUTERS
from shared.crypto_intent_bridges import CRYPTO_ROUTERS
from shared.risk.routes import router as risk_router
from shared.vrl import router as vrl_router
from shared.quantum_routes import router as quantum_router
from shared.personalities_routes import router as personalities_router
from shared.hypothesis import router as hypothesis_router
from shared.mc_shelly import router as mc_shelly_router
from shared.patches import router as patches_router
from shared.runtime.routes import router as platform_survival_router
from shared.runtime.sidecar_checkin import router as sidecar_checkin_router
from shared.calibration.confidence_floor_sweep import (
    router as confidence_floor_sweep_router,
)
from shared.calibration.snapshot_completeness import (
    router as snapshot_completeness_router,
)
from shared.lane_execution import router as lane_execution_router
from shared.coordinator.routes import router as coordinator_router
from shared.runtime_bundles import router as runtime_bundles_router
from shared.promotion_artifact_report import (
    router as promotion_artifact_report_router,
)
from shared.public_api.news import router as public_news_router
from shared.public_api.dark_pool import router as public_darkpool_router
from shared.observation_receipts import router as observation_receipts_router
from shared.learning_ladder import router as learning_ladder_router
from shared.shelly_bus.mc_shelly_ingest import router as shelly_bus_router
from shelly import router as shelly_router

from routes.memory_kernel_routes import router as memory_kernel_router
from routes.orphan_inspection_routes import router as orphan_inspection_router
from routes.orphan_replay_routes import router as orphan_replay_router
from routes.broker_freeze_routes import router as broker_freeze_router
from routes.broker_reconcile_routes import router as broker_reconcile_router
from routes.sidecar_diagnostics import router as sidecar_diagnostics_router
from routes.data_stack_admin import router as data_stack_admin_router
from routes.market_data_keys import router as market_data_keys_router
from routes.opinion_silence_watchdog import (
    router as opinion_silence_watchdog_router,
)
from routes.heartbeat_reconciler_admin import (
    router as heartbeat_reconciler_admin_router,
)
from routes.brain_outages import router as brain_outages_router
from routes.brain_health import router as brain_health_router
from routes.market_data_snapshot import router as market_data_snapshot_router
from routes.brain_runtime import router as brain_runtime_router
from routes.daily_snapshots import router as daily_snapshots_router
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
from routes.brain_doctrine_hint import router as brain_doctrine_hint_router
from routes.intent_inspect import router as intent_inspect_router
from routes.storage_rollup import router as storage_rollup_router
from routes.trading_controls import router as trading_controls_router
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
from routes.admin_intents_post_mortem import (
    router as admin_intents_post_mortem_router,
)
from routes.admin_intents_funnel import (
    router as admin_intents_funnel_router,
)
from routes.admin_hot_brain_router import (
    router as admin_hot_brain_router,
)
from routes.admin_spread_quality import (
    router as admin_spread_quality_router,
)
from routes.admin_brain_metrics import (
    router as admin_brain_metrics_router,
)
from routes.intent_why import router as intent_why_router
from routes.seat_state_diagnose import router as seat_state_diagnose_router
from routes.webull_caps_admin import router as webull_caps_admin_router
from routes.exposure_caps_admin import router as exposure_caps_admin_router
from routes.equity_extended_hours_admin import router as equity_extended_hours_admin_router
from routes.canary_admin import router as canary_admin_router
from routes.brain_tuning_admin import router as brain_tuning_admin_router
from routes.pipeline_blocker_histogram import router as pipeline_blocker_histogram_router
from routes.admin_auto_submit import router as admin_auto_submit_router
from routes.admin_quiver import router as admin_quiver_router
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
from routes.finnhub_backfill import router as finnhub_backfill_router
from routes.learning_scoreboard import router as learning_scoreboard_router
from routes.runtime_broker_status import router as runtime_broker_status_router
from routes.runtime_position_close import router as runtime_position_close_router
from routes.runtime_cross_brain_memories import (
    router as cross_brain_memories_router,
)
from routes.admin_brackets import router as admin_brackets_router
from routes.research import router as research_router
from routes.verifier import router as verifier_router

from runtimes.alpha.routes import router as alpha_router
from runtimes.camaro.routes import router as camaro_router
from runtimes.chevelle.routes import router as chevelle_router


def register_routers(api_router: APIRouter) -> None:
    """Attach every sub-router to the parent `api_router`.

    Order matches the original `server.py` 1:1. Adding a new router:
    drop the import above and add the `include_router(...)` call at
    the end of this function — same convention the old file used.
    """
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
    api_router.include_router(seat_performance_router)
    api_router.include_router(technicals_router)
    api_router.include_router(kraken_router)
    api_router.include_router(ibkr_router)
    api_router.include_router(public_router)
    api_router.include_router(roster_router)
    api_router.include_router(promotion_router)
    # `doctrine_router` here is the shared.doctrine umbrella router
    # (NOT shared.doctrine_routes — that's a different file). The
    # original server.py imported both and shadowed the name; we
    # keep them as `doctrine_legacy_router` and `doctrine_router`
    # below to avoid the name collision while preserving order.
    api_router.include_router(doctrine_legacy_router)
    api_router.include_router(intents_router)
    api_router.include_router(executor_router)
    api_router.include_router(auditor_router)
    api_router.include_router(seat_nudges_router)
    api_router.include_router(execution_router)
    api_router.include_router(admin_wrappers_router)
    api_router.include_router(admin_intents_post_mortem_router)
    api_router.include_router(admin_intents_funnel_router)
    api_router.include_router(admin_hot_brain_router)
    api_router.include_router(admin_spread_quality_router)
    api_router.include_router(admin_brain_metrics_router)
    api_router.include_router(intent_why_router)
    api_router.include_router(seat_state_diagnose_router)
    api_router.include_router(webull_caps_admin_router)
    api_router.include_router(exposure_caps_admin_router)
    api_router.include_router(equity_extended_hours_admin_router)
    api_router.include_router(canary_admin_router)
    api_router.include_router(brain_tuning_admin_router)
    api_router.include_router(pipeline_blocker_histogram_router)
    api_router.include_router(admin_auto_submit_router)
    api_router.include_router(admin_quiver_router)
    api_router.include_router(paradox_v2_router)
    api_router.include_router(live_positions_router)
    api_router.include_router(brain_lane_policy_router)
    api_router.include_router(redeye_bridge_router)
    api_router.include_router(chevelle_bridge_router)
    # Equity bridges for all four brains (camino, barracuda, hellcat, gto).
    # Generated via `shared.intent_bridge_factory.make_intent_bridge`.
    for _eq_router in EQUITY_ROUTERS:
        api_router.include_router(_eq_router)
    # Crypto bridges for camino + barracuda (GTO + Hellcat already
    # have legacy crypto bridges above). Same factory, lane=crypto.
    for _cr_router in CRYPTO_ROUTERS:
        api_router.include_router(_cr_router)
    api_router.include_router(risk_router)
    api_router.include_router(vrl_router)
    api_router.include_router(hypothesis_router)
    api_router.include_router(mc_shelly_router)
    api_router.include_router(patches_router)
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
    api_router.include_router(finnhub_backfill_router)
    api_router.include_router(brain_runtime_router)
    api_router.include_router(shelly_router)
    api_router.include_router(brain_memory_ingest_router)
    api_router.include_router(learning_scoreboard_router)
    api_router.include_router(runtime_broker_status_router)
    api_router.include_router(runtime_position_close_router)
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
    api_router.include_router(admin_brackets_router)
    api_router.include_router(quantum_router)
    api_router.include_router(personalities_router)
    api_router.include_router(flags_router)
    api_router.include_router(alpha_router)
    api_router.include_router(camaro_router)
    api_router.include_router(chevelle_router)
    api_router.include_router(storage_rollup_router)
    api_router.include_router(trading_controls_router)
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
    # Research Layer — read-only Strategy Lab. NEVER routes orders;
    # only enriches intents with evidence the brain can opine on.
    api_router.include_router(research_router)
    # Verifier — Lessons, Brain Report Cards, and the Setup Memory
    # confidence-adjuster kill switch. All read-only or admin-only.
    api_router.include_router(verifier_router)
