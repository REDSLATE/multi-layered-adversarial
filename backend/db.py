"""MongoDB connection + index management.
Single shared client. Namespaced collection names enforced via collections.py."""
import os
from motor.motor_asyncio import AsyncIOMotorClient

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]


async def ensure_indexes() -> None:
    # Auth
    await db.users.create_index("email", unique=True)
    await db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)
    await db.login_attempts.create_index("identifier")

    # Shared infrastructure
    await db.shared_adl_receipts.create_index([("runtime", 1), ("timestamp", -1)])
    await db.shared_adl_receipts.create_index([("role_violation", 1), ("timestamp", -1)])
    await db.shared_labeled_memories.create_index([("runtime", 1), ("timestamp", -1)])
    await db.shared_calibrators.create_index([("runtime", 1), ("name", 1)])
    await db.shared_feature_builders.create_index("name", unique=True)
    await db.shared_artifact_inventory.create_index([("runtime", 1), ("artifact", 1)])

    # Per-runtime decision/shadow stores (kept ISOLATED, never cross-read)
    await db.alpha_decision_log.create_index([("timestamp", -1)])
    await db.camaro_shadow_rows.create_index([("timestamp", -1)])
    await db.chevelle_memory_labels.create_index([("timestamp", -1)])

    # Heartbeats (one row per runtime, upserted)
    await db.shared_heartbeats.create_index("runtime", unique=True)

    # Authority + promotion
    await db.shared_authority_state.create_index("runtime", unique=True)
    await db.shared_promotion_artifacts.create_index([("runtime", 1), ("emitted_at", -1)])
    await db.shared_promotion_artifacts.create_index("artifact_id", unique=True)
    await db.shared_promotion_proposals.create_index([("runtime", 1), ("status", 1), ("created_at", -1)])
    await db.shared_promotion_proposals.create_index("proposal_id", unique=True)

    # Shared technical evidence (OHLCV + indicators)
    await db.shared_ohlcv_bars.create_index(
        [("source", 1), ("symbol", 1), ("tf", 1), ("ts", -1)],
        unique=True,
    )
    await db.shared_ohlcv_bars.create_index([("symbol", 1), ("tf", 1), ("ts", -1)])
    await db.shared_indicator_snapshots.create_index(
        [("source", 1), ("symbol", 1), ("tf", 1)],
        unique=True,
    )

    # Kraken connection — singleton credential doc + append-only audit log
    await db.kraken_audit_log.create_index([("ts", -1)])

    # Brain roster — append-only audit log of role assignments
    await db.roster_audit_log.create_index([("ts", -1)])

    # IBKR connection — singleton credential + append-only audit log
    await db.ibkr_audit_log.create_index([("ts", -1)])

    # ── Hypothesis engine / Brain Recall — heavy read path on /admin/hypothesis ──
    # These indexes turn the per-role queries from collection scans into
    # bounded lookups. Profile-driven (see /api/hypothesis/_perf for p50/p95/p99).
    await db.shared_intents.create_index([("stack", 1), ("symbol", 1), ("ingest_ts", -1)])
    await db.shared_intents.create_index([("stack", 1), ("executed", 1), ("executed_at", -1)])
    await db.shared_brain_opinions.create_index([("runtime", 1), ("topic", 1), ("posted_at", -1)])
    await db.shared_brain_outcomes.create_index([("opinion_id", 1), ("resolved_at", -1)])
    # Shelly memory regex search was the worst offender (~25-40ms scan);
    # a TEXT index pivots it to indexed token lookup.
    try:
        await db.shared_labeled_memories.create_index(
            [("payload_summary", "text"), ("reason", "text")],
            name="shelly_payload_text_idx",
        )
    except Exception:  # noqa: BLE001 - text index may already exist with different fields
        pass
    # Hypothesis audit-log queries by recency
    await db.hypothesis_analyses.create_index([("generated_at", -1)])
    await db.hypothesis_analyses.create_index([("symbol", 1), ("generated_at", -1)])
    # Executor/Auditor rotation audit logs queried by ts desc
    await db.shared_executor_rotations.create_index([("ts", -1)])
    await db.shared_auditor_rotations.create_index([("ts", -1)])

    # ── MC Shelly — Mission Control's labeled memory store ──────────────
    # Operator queries: slice by event_type, position_at_event, brain,
    # symbol, outcome, ts window. These indexes cover those.
    await db.mc_shelly.create_index([("ts", -1)])
    await db.mc_shelly.create_index([("event_type", 1), ("ts", -1)])
    await db.mc_shelly.create_index([("position_at_event", 1), ("ts", -1)])
    await db.mc_shelly.create_index([("brain", 1), ("ts", -1)])
    await db.mc_shelly.create_index([("symbol", 1), ("ts", -1)])
    await db.mc_shelly.create_index([("ref_id", 1)])
