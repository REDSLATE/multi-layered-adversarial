"""MongoDB connection + index management.
Single shared client. Namespaced collection names enforced via collections.py."""
import os
from motor.motor_asyncio import AsyncIOMotorClient

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]


async def ensure_indexes() -> None:
    # ── Consensus pool indexes (2026-06-24) ───────────────────────
    # Non-executor brains' opinions land in `intent_consensus_pool`.
    # The seat policy reads it by (lane, symbol, ts) and writes by
    # appending. TTL 900s = 15 min matches the lookup window.
    await db.intent_consensus_pool.create_index(
        [("lane", 1), ("symbol", 1), ("ts", -1)],
        name="consensus_pool_lookup_idx",
    )
    await db.intent_consensus_pool.create_index(
        "ts",
        expireAfterSeconds=900,
        name="consensus_pool_ttl_15m",
    )
    # Sidecar telemetry (per-executor-intent boost record). Same TTL.
    await db.intent_consensus_telemetry.create_index(
        "intent_id",
        name="consensus_telemetry_intent_idx",
    )
    await db.intent_consensus_telemetry.create_index(
        "ts",
        expireAfterSeconds=900,
        name="consensus_telemetry_ttl_15m",
    )
    await db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)

    # ── login_attempts (brute-force tracker) ──────────────────────
    # Doctrine pin (2026-06-24): two prod hotfixes here.
    #
    # 1. The login route reads with the filter
    #       {identifier, success=False, ts >= cutoff}
    #    and we ONLY had `identifier_1` before. Within each bucket the
    #    other two fields were filtered in memory — fine when fresh,
    #    catastrophic after weeks of bot scans against the admin
    #    email. As the bucket grew the count_documents call started
    #    exceeding the gateway request deadline, surfacing as the
    #    operator-visible "intermittent HTTP 502 on sign-in that
    #    gets worse over time" symptom. The compound index below
    #    covers the read in full.
    #
    # 2. We did NOT have a TTL index, so rows lived forever. The
    #    `ts` field was previously stored as an ISO STRING (not a
    #    BSON Date), so even adding a TTL would have been a no-op
    #    (TTL only works on Date fields). We now write `ts` as a
    #    Date in auth.py, prune any legacy string-typed rows below,
    #    and add the TTL index here. 900s = 15min, matching the
    #    brute-force window itself; anything older is useless.
    try:
        # Legacy single-field index — keep around for safe migration;
        # the new compound index supersedes it for the read query.
        await db.login_attempts.create_index("identifier")
    except Exception:  # noqa: BLE001
        pass
    await db.login_attempts.create_index(
        [("identifier", 1), ("success", 1), ("ts", 1)],
        name="login_attempts_lockout_idx",
    )
    await db.login_attempts.create_index(
        "ts",
        expireAfterSeconds=900,
        name="login_attempts_ttl_15m",
    )
    # One-shot: purge legacy string-typed `ts` rows. TTL doesn't apply
    # to them (TTL only works on Date), so without this they'd persist
    # forever and continue dragging the bucket. Idempotent — once the
    # collection has no string-ts rows, future calls are no-ops.
    try:
        await db.login_attempts.delete_many({"ts": {"$type": "string"}})
    except Exception:  # noqa: BLE001
        pass

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

    # Sidecar check-ins (Portable Survival Layer) — one row per runtime,
    # upserted; carries the latest RuntimeStamp + validation verdict.
    await db.sidecar_checkins.create_index("runtime", unique=True)

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
    # 2026-06-22 (P0 prod hotfix): `/api/intents` lists are filtered
    # by `stack`/`symbol`/`lane`/`gate_state` (all optional). When the
    # operator clears all filters — which the default Intents page
    # does on first load — the query becomes `find({}).sort("ingest_ts",
    # -1)`. The compound index above is leading-on-`stack` so the
    # mongo planner CAN'T use it for the unfiltered case; the sort
    # falls back to an in-memory blocking sort which on prod (~100k+
    # intents) trips the 32MB sort limit and returns HTTP 500.
    # Preview never reproduced it because preview has ≤1k intents.
    # Solo index on `ingest_ts` is the surgical fix — bounded
    # memory, indexed sort.
    await db.shared_intents.create_index([("ingest_ts", -1)], name="shared_intents_ingest_ts_idx")
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

    # ── sovereign_state_history ──
    # Doctrine pin (2026-05-26): converted from TTL-DELETE to
    # storage-rollup. Rows older than 60d are compacted to slim
    # `{movement, event}`-labeled rollup rows via
    # `shared/storage_rollup/` (verbose original purged 7d after
    # rollup). The previous 30d TTL-delete index
    # (`sovereign_history_ttl_30d`) is dropped by
    # `scripts/drop_sovereign_history_ttl.py` once the operator
    # confirms the rollup pipeline is healthy on prod. We DO NOT
    # auto-drop here — it's the operator's call.
    #
    # The Date field `received_at_dt` continues to be stamped on
    # every new history row (see `shared/sovereign_mode_guard.py`)
    # because the rollup runner queries it as `ts_field`.
    pass
