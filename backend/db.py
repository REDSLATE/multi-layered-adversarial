"""MongoDB connection + index management.
Single shared client. Namespaced collection names enforced via collections.py.

Doctrine pin (2026-02-26 post-mortem of failed prod deploy at 00:28 UTC):
NEVER `await ensure_indexes()` inside the FastAPI lifespan handler.
On prod, `shared_intents` and `shared_gate_results` are multi-million-row
collections; new compound-index builds take minutes server-side.
pymongo's socket timeout (~15s) is much shorter — `await create_index`
raises NetworkTimeout, the lifespan crashes, the pod never reaches Ready,
all API responses become Cloudflare 520. The fix:

  * Lifespan fires `ensure_indexes()` as a background task and returns
    immediately (see server_modules/lifespan.py).
  * `_safe_create_index()` enforces a 6s client-side deadline per index
    and swallows NetworkTimeout/ExecutionTimeout. Mongo continues
    building in the background regardless.
  * `POST /api/admin/db/ensure-indexes` runs the FULL ensure() with
    per-index status (created / exists / timeout / error) so the
    operator can see what's done and what's still building.
"""
import asyncio
import logging
import os
import time
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import (
    ExecutionTimeout,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
)

mongo_url = os.environ["MONGO_URL"]

# Connection-pool config (2026-02-27 prod hotfix — Kraken loop & 520s).
# Updated 2026-06-30 — softened timeouts after prod 500s.
#
# The previous tight timeouts (`serverSelectionTimeoutMS=15s`,
# `waitQueueTimeoutMS=10s`) were too aggressive for the Atlas shared
# tier under real load: on transient slow-selection (Atlas autoscale
# events, replica election, region failover), EVERY request would
# throw a `ServerSelectionTimeoutError` and the user saw HTTP 500s
# across the entire backend.
#
# Going back closer to pymongo defaults, keeping only the changes
# that fix the documented "connection pool paused" Atlas symptom:
#   * `retryWrites=True` + `retryReads=True` — auto-retry on the
#     transient SocketException / "connection pool paused".
#   * `maxIdleTimeMS=45_000` — recycle idle conns BEFORE Atlas
#     drops them (Atlas idle-kill is ~60s on shared tier). This is
#     the ONE change that actually fixed the symptom.
#   * `maxPoolSize` left at pymongo default (100) so we never
#     starve under burst load.
#   * `serverSelectionTimeoutMS` left at pymongo default (30s) so
#     transient Atlas slowness doesn't 500-cascade.
#   * `appname="risedual-mc"` — diagnostic only; helps identify the
#     workload in Atlas dashboards. No timeout impact.
client = AsyncIOMotorClient(
    mongo_url,
    retryWrites=True,
    retryReads=True,
    maxIdleTimeMS=45_000,
    appname="risedual-mc",
)
db = client[os.environ["DB_NAME"]]

logger = logging.getLogger("risedual.db")


# Per-index status, updated by `_safe_create_index`. Read by the
# admin endpoint so the operator sees a JSON report instead of just
# "ok: true". Keys are index names, values are dicts:
#   {"status": "created" | "exists" | "timeout" | "error",
#    "collection": str,
#    "elapsed_ms": int,
#    "reason": str | None}
_INDEX_REPORT: dict[str, dict] = {}


def get_index_report() -> dict:
    """Snapshot of the most recent ensure_indexes run, as a dict so
    the admin endpoint can serialize it. Returns an empty dict if
    no run has happened yet."""
    return dict(_INDEX_REPORT)


async def _safe_create_index(coll, keys, *, deadline_s: float = 6.0, **opts) -> dict:
    """Create-index wrapper that NEVER blocks startup.

    `deadline_s` is the client-side wait ceiling. Default 6s for
    startup-callable paths; the admin endpoint may pass a larger
    value (e.g. 60s) for operator-driven manual rebuilds.

    Updates `_INDEX_REPORT[name]` with per-index outcome so the
    admin endpoint can return a structured JSON report.
    """
    name = opts.get("name") or "_".join(f"{k[0]}_{k[1]}" for k in keys)
    started = time.monotonic()
    try:
        await asyncio.wait_for(coll.create_index(keys, **opts), timeout=deadline_s)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Sub-50ms means Mongo no-op'd (index already existed).
        # Anything longer means it actually built (or partially built).
        status = "exists" if elapsed_ms < 50 else "created"
        _INDEX_REPORT[name] = {
            "status": status, "collection": coll.name,
            "elapsed_ms": elapsed_ms, "reason": None,
        }
        return _INDEX_REPORT[name]
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "create_index %s on %s exceeded %.1fs client deadline; "
            "Mongo continues building in the background. Safe.",
            name, coll.name, deadline_s,
        )
        _INDEX_REPORT[name] = {
            "status": "timeout", "collection": coll.name,
            "elapsed_ms": elapsed_ms,
            "reason": f"client_deadline_{deadline_s}s_mongo_building_async",
        }
        return _INDEX_REPORT[name]
    except (NetworkTimeout, ExecutionTimeout, ServerSelectionTimeoutError) as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "create_index %s on %s server-timeout (%s); Mongo continues "
            "in background. Safe.",
            name, coll.name, type(exc).__name__,
        )
        _INDEX_REPORT[name] = {
            "status": "timeout", "collection": coll.name,
            "elapsed_ms": elapsed_ms, "reason": type(exc).__name__,
        }
        return _INDEX_REPORT[name]
    except OperationFailure as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "create_index %s on %s OperationFailure code=%s msg=%s",
            name, coll.name, exc.code, str(exc)[:200],
        )
        _INDEX_REPORT[name] = {
            "status": "error", "collection": coll.name,
            "elapsed_ms": elapsed_ms,
            "reason": f"op_failure_code_{exc.code}",
        }
        return _INDEX_REPORT[name]
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "create_index %s on %s unexpected %s: %s",
            name, coll.name, type(exc).__name__, str(exc)[:200],
        )
        _INDEX_REPORT[name] = {
            "status": "error", "collection": coll.name,
            "elapsed_ms": elapsed_ms, "reason": type(exc).__name__,
        }
        return _INDEX_REPORT[name]


async def ensure_indexes(*, heavy_deadline_s: float = 6.0) -> None:
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
    # Sidecar telemetry (per-executor-intent boost record).
    # TTL doctrine pin (2026-06-24 op-spec): the Brain Metrics tile's
    # consensus_boost_applied_rate KPI queries this sidecar over the
    # operator-chosen window (1-168h). The pool itself stays at 15min
    # because it drives the actual boost; the sidecar is observability
    # so it lives 7d to support the full metric window range.
    await db.intent_consensus_telemetry.create_index(
        "intent_id",
        name="consensus_telemetry_intent_idx",
    )
    # Drop the legacy 15-min TTL if present (idempotent — Mongo can't
    # mutate expireAfterSeconds on an existing index, so we have to
    # drop+recreate when the value changes).
    try:
        idx_info = await db.intent_consensus_telemetry.index_information()
        legacy = idx_info.get("consensus_telemetry_ttl_15m")
        if legacy and legacy.get("expireAfterSeconds") != 604800:
            await db.intent_consensus_telemetry.drop_index(
                "consensus_telemetry_ttl_15m"
            )
    except Exception:  # noqa: BLE001
        pass
    await db.intent_consensus_telemetry.create_index(
        "ts",
        expireAfterSeconds=604800,    # 7 days
        name="consensus_telemetry_ttl_7d",
    )
    # Also drop the legacy name if the new one already exists with
    # the right TTL (cleanup for any partial-migration state).
    try:
        idx_info = await db.intent_consensus_telemetry.index_information()
        if (
            "consensus_telemetry_ttl_7d" in idx_info
            and "consensus_telemetry_ttl_15m" in idx_info
        ):
            await db.intent_consensus_telemetry.drop_index(
                "consensus_telemetry_ttl_15m"
            )
    except Exception:  # noqa: BLE001
        pass
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

    # Users — unique index on email. Without this, every login does
    # a full collection scan on users. Cheap fix, real win once the
    # users collection has more than a handful of rows. Unique
    # constraint also guards against accidental duplicate-admin seeds.
    # Created with `unique=True` — if a duplicate already exists from
    # legacy data, the index creation will raise; swallowed safely
    # so startup doesn't crash and we surface the issue via logs.
    try:
        await db.users.create_index("email", unique=True, name="users_email_unique")
    except Exception as e:  # noqa: BLE001
        # Log but don't crash — a duplicate-email row would block
        # the unique index. We'd rather start up degraded than fail
        # the boot.
        import logging
        logging.getLogger("risedual.db").warning(
            "users.email unique index creation failed: %s", e,
        )

    # Shared infrastructure
    await db.shared_adl_receipts.create_index([("runtime", 1), ("timestamp", -1)])
    await db.shared_adl_receipts.create_index([("role_violation", 1), ("timestamp", -1)])
    await db.shared_labeled_memories.create_index([("runtime", 1), ("timestamp", -1)])
    await db.shared_calibrators.create_index([("runtime", 1), ("name", 1)])
    await db.shared_feature_builders.create_index("name", unique=True)
    await db.shared_artifact_inventory.create_index([("runtime", 1), ("artifact", 1)])

    # Shared opinions tape (BrainConsole `/api/shared/opinions` and
    # conflict aggregation) — 2026-07-11 hotfix. Prod symptom: the
    # endpoint would timeout on Atlas with "NetworkTimeout: read
    # operation timed out" because `find({runtime:X}).sort(posted_at:-1)`
    # was doing a full collection scan on a growing tape. The
    # compound `(runtime, posted_at desc)` index makes this an O(log
    # n) index-only lookup and the endpoint returns in <50ms.
    await _safe_create_index(
        db.shared_opinions,
        [("runtime", 1), ("posted_at", -1)],
        name="shared_opinions_runtime_posted_at",
    )
    await _safe_create_index(
        db.shared_opinions,
        [("topic", 1), ("posted_at", -1)],
        name="shared_opinions_topic_posted_at",
    )
    await _safe_create_index(
        db.shared_opinions,
        [("thread_root", 1), ("posted_at", -1)],
        name="shared_opinions_thread_posted_at",
    )
    await _safe_create_index(
        db.shared_opinions,
        [("posted_at", -1)],
        name="shared_opinions_posted_at",
    )

    # MC Seat Arbiter — 2026-07-11 (iter-26). One row per
    # (seat_key, brain) so all four brains competing for the same
    # 5-min bucket surface with a single seat_key lookup. TTL 30
    # days on `recorded_at` — the mc_seats tape is meant to be
    # short-term arbitration + grading history, not a permanent
    # archive (that lives on `shared_intents` for winners and
    # `counterfactual_signals` for the graded blockeds).
    await _safe_create_index(
        db.mc_seats,
        [("seat_key", 1), ("brain", 1)],
        name="mc_seats_seat_brain",
        unique=True,
    )
    await _safe_create_index(
        db.mc_seats,
        [("brain", 1), ("lane", 1), ("ts", -1)],
        name="mc_seats_brain_lane_ts",
    )
    # Grader read pattern: find opinions old enough to grade at a
    # given horizon that haven't been graded yet.
    await _safe_create_index(
        db.mc_seats,
        [("ts", 1)],
        name="mc_seats_ts_grader",
    )
    # MC Pulse — 2026-07-11 (iter-27). Pulse-owned envelopes carry
    # a `pulse_id` that lets retried pulses complete missing work
    # without ever double-executing. Two unique constraints
    # enforce that:
    #   (pulse_id, brain, symbol, lane) on `mc_seats`     — at most
    #                                                       one row
    #                                                       per (pulse,
    #                                                       brain, sym,
    #                                                       lane).
    #   (pulse_id, brain, symbol, lane) on `mc_opinions_compare`
    #     — same shape on the migration comparison tape (steps 2-5).
    #   `mc_pulses` receipts — primary-key indexed by pulse_id.
    await _safe_create_index(
        db.mc_seats,
        [("pulse_id", 1), ("brain", 1), ("symbol", 1), ("lane", 1)],
        name="mc_seats_pulse_brain_symbol_lane",
        unique=True,
        sparse=True,   # legacy rows written before pulse_id existed
                       # have no pulse_id — sparse keeps them valid
    )
    await _safe_create_index(
        db.mc_opinions_compare,
        [("pulse_id", 1), ("brain", 1), ("symbol", 1), ("lane", 1)],
        name="mc_opinions_compare_pulse_brain_symbol_lane",
        unique=True,
    )
    await _safe_create_index(
        db.mc_opinions_compare,
        [("brain", 1), ("evaluated_at", -1)],
        name="mc_opinions_compare_brain_evaluated_at",
    )
    await _safe_create_index(
        db.mc_pulses,
        [("started_at", -1)],
        name="mc_pulses_started_at",
    )

    # ── 2026-07-12: parity trend snapshots (DEPRECATED post-P4) ──
    # Kept only so already-persisted rows fetch cleanly during the
    # BC-alias window. TTL 30d — will decay out naturally. The
    # canonical collection going forward is `mc_pulse_health_snapshots`.
    await _safe_create_index(
        db.mc_parity_snapshots,
        [("brain", 1), ("at", -1)],
        name="mc_parity_snapshots_brain_at",
    )
    await _safe_create_index(
        db.mc_parity_snapshots,
        [("at", 1)],
        name="mc_parity_snapshots_ttl",
        expireAfterSeconds=30 * 86400,
    )

    # ── 2026-07-12 P4: pulse-health trend snapshots ──
    # Rolling per-brain pulse-health metrics (post-runner-deletion
    # semantics). Compound (brain, at) supports the history read
    # pattern (newest-first per brain). TTL 30d.
    await _safe_create_index(
        db.mc_pulse_health_snapshots,
        [("brain", 1), ("at", -1)],
        name="mc_pulse_health_snapshots_brain_at",
    )
    await _safe_create_index(
        db.mc_pulse_health_snapshots,
        [("at", 1)],
        name="mc_pulse_health_snapshots_ttl",
        expireAfterSeconds=30 * 86400,
    )

    # MC parity manifests (2026-02, parity step 1/2). Unique
    # (parity_key, path) — the runner and pulse rows for the SAME
    # bar close MUST coexist so the parity endpoint can pair them;
    # a unique-on-parity_key alone would let one path overwrite the
    # other, destroying exactly the data parity is meant to
    # produce. TTL 7d — parity is short-window work.
    await _safe_create_index(
        db.mc_parity_manifests,
        [("parity_key", 1), ("path", 1)],
        name="mc_parity_manifests_key_path",
        unique=True,
    )
    await _safe_create_index(
        db.mc_parity_manifests,
        [("brain_id", 1), ("symbol", 1), ("source_bar_close_at", -1)],
        name="mc_parity_manifests_brain_symbol_ts",
    )
    await _safe_create_index(
        db.mc_parity_manifests,
        [("recorded_at", 1)],
        name="mc_parity_manifests_ttl",
        expireAfterSeconds=7 * 86400,
    )

    # ── P1 (2026-02-11): mc_brain_silences diagnostic sidecar ──
    # One row per (pulse, brain, snapshot) silence. NOT used for the
    # aggregate no_data_breakdown (that reads from `mc_pulses`); this
    # collection is for ad-hoc operator queries like "why was camino
    # silent on NVDA between 14:00 and 14:15?". 7-day TTL matches the
    # operator's investigation-window memory.
    await _safe_create_index(
        db.mc_brain_silences,
        [("brain_id", 1), ("at", -1)],
        name="mc_brain_silences_brain_at",
    )
    await _safe_create_index(
        db.mc_brain_silences,
        [("symbol", 1), ("at", -1)],
        name="mc_brain_silences_symbol_at",
    )
    await _safe_create_index(
        db.mc_brain_silences,
        [("reason", 1), ("at", -1)],
        name="mc_brain_silences_reason_at",
    )
    await _safe_create_index(
        db.mc_brain_silences,
        [("at", 1)],
        name="mc_brain_silences_ttl",
        expireAfterSeconds=7 * 86400,
    )

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

    # ── Paradox v3 intent_watch_queue (2026-02, Step 3 — DORMANT) ──
    # New collection for v3 WAIT_FOR_TRIGGER plans. Scanned by
    # shared.pipeline.trigger_watcher when the operator opts in via
    # PARADOX_V3_TRIGGER_WATCHER=1. Indexes here are boot-time so
    # the watcher's first tick is index-backed regardless of when
    # the env flag flips.
    await db.intent_watch_queue.create_index([("state", 1), ("queued_at", 1)])
    await db.intent_watch_queue.create_index([("symbol", 1), ("lane", 1), ("state", 1)])
    await db.intent_watch_queue.create_index([("intent_id", 1)], unique=True)
    # TTL safety net — orphan rows older than 30 days auto-prune.
    # The per-plan ttl_seconds expires rows actively via the watcher;
    # this index is the back-stop so abandoned queues don't bloat.
    # Mongo TTL requires a BSON Date field — `queued_at` is stamped
    # via `datetime.now(timezone.utc)` (BSON Date), not ISO string.
    await db.intent_watch_queue.create_index(
        "queued_at",
        expireAfterSeconds=30 * 86_400,
        name="intent_watch_queue_ttl_30d",
    )

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
    # 2026-02-25 (P0 prod hotfix — regression of 2026-06-22 hotfix):
    # On 2026-02-23 the default sort changed from `ingest_ts` →
    # `conviction` (= `[(confidence, -1), (ingest_ts, -1)]`). The
    # 2026-06-22 `ingest_ts`-only index above no longer covers the
    # new hot path: with ~100k+ prod intents the planner blocking-
    # sorts in memory → 32MB cap → Mongo socket timeout → operator
    # sees `NetworkTimeout` 500 on the Intents page.
    # Two compound indexes cover the realistic query shapes:
    #   1. unfiltered conviction sort (when include_disabled_lanes=true
    #      or both lanes enabled with no lane pin) — index on
    #      (confidence -1, ingest_ts -1).
    #   2. default page (include_disabled_lanes=false, lane $in
    #      [enabled]) — leading-on-lane lets the planner intersect
    #      filter + sort in a single index scan.
    await db.shared_intents.create_index(
        [("confidence", -1), ("ingest_ts", -1)],
        name="shared_intents_conviction_idx",
    )
    await db.shared_intents.create_index(
        [("lane", 1), ("confidence", -1), ("ingest_ts", -1)],
        name="shared_intents_lane_conviction_idx",
    )
    # 2026-02-25 (later — admin diagnostic 500 sweep):
    # The Intents page hotfix earlier today is one tile in a tile farm.
    # An operator screenshot showed ~8 OTHER admin tiles 500ing on the
    # same MongoDB-timeout pattern. Survey traced them to six query
    # shapes across five collections that had ZERO covering indexes.
    # This block adds the missing compound indexes — surgical, named,
    # idempotent. Boot-time `ensure_indexes()` creates them on every
    # restart (Mongo no-ops if they already exist).
    #
    # `admin_brain_input_health` joins per-brain emit stats via
    # `stack_canonical + created_at` — find_one(sort) and aggregate
    # group both run blocking sorts at prod volumes today.
    await db.shared_intents.create_index(
        [("stack_canonical", 1), ("created_at", -1)],
        name="shared_intents_stack_canonical_created_idx",
    )
    # 2026-07-09 (P1 prod hotfix — Barracuda in-process status timed out):
    # `_build_in_process_status` (routes/brain_runtime.py) runs:
    #   * count_documents({stack_canonical, ingest_ts: {$gte: cutoff}})   × 2
    #   * aggregate([$match({stack_canonical, ingest_ts: {$gte: cutoff}}), $group])
    #   * find_one({stack_canonical}, sort=[(ingest_ts, -1)])
    # The existing `(stack_canonical, created_at)` and `(ingest_ts)` indexes
    # BOTH miss the composite `(stack_canonical, ingest_ts)` shape. On the
    # brain with the longest history (Barracuda, post-camaro migration),
    # Atlas timed out the whole status endpoint. This composite lets the
    # cursor use a covered range scan on ingest_ts within a fixed
    # stack_canonical partition — O(logN + k) instead of full scan.
    await db.shared_intents.create_index(
        [("stack_canonical", 1), ("ingest_ts", -1)],
        name="shared_intents_stack_canonical_ingest_ts_idx",
    )
    # 2026-02-26 (P0 prod hotfix — auto_router_loop stalled):
    # `shared/auto_router.py::_tick()` queries
    #   find({executed:$ne True, action:$in [BUY/SELL/SHORT/COVER],
    #         symbol:$ne None, gate_state:$nin [...]}).sort(created_at, 1)
    # The 5 pre-existing `shared_intents` indexes all sort on
    # `ingest_ts -1`, none on `created_at 1`. So the auto-router was
    # doing a full COLLSCAN + in-memory sort on every tick. On prod
    # (millions of rows) this exceeded Mongo's maxTimeMS and the task
    # crashed every tick → tick_count=0 → no orders fired.
    # `(action, created_at)` is the surgical fix: `action $in [...]`
    # uses the index for a bounded multi-key lookup, `created_at` is
    # the sort key. `$ne`/`$nin` on executed/gate_state are evaluated
    # inline against the much smaller post-action set (typically <1%
    # of total intents because most are HOLD).
    #
    # USES `_safe_create_index` with the function-scoped
    # `heavy_deadline_s` (default 6s for startup; admin endpoint
    # passes 60s for operator-driven manual rebuilds).
    await _safe_create_index(
        db.shared_intents,
        [("action", 1), ("created_at", 1)],
        deadline_s=heavy_deadline_s,
        name="shared_intents_action_created_idx",
    )
    # 2026-02-28 (P0 prod hotfix — funnel + auto_router NetworkTimeout):
    # The intent-clearance funnel does `count_documents({ingest_ts:
    # {$gte: X}, lane: <L>})` and the auto-router `_tick` scans
    # `find({ingest_ts: {$gte: X}, action: $in [...], gate_state:
    # $nin [...]})`. On prod's multi-million-row `shared_intents`,
    # neither had a covering index for the WINDOW+LANE combination —
    # each query fell back to a collection scan that exceeded the
    # 15s Atlas socket timeout. Symptom: crypto stopped firing on
    # prod for 2+ days AND the funnel returned
    # `NetworkTimeout: ...:27017: The read operation timed out`.
    #
    # This compound index covers both callers:
    #   * `lane` first (equality filter — perfect for index prefix)
    #   * `ingest_ts` desc (matches the funnel's `$gte` range +
    #     the auto-router's descending sort in one motion)
    #   * `action` last (auto-router's `$in [BUY/SELL/SHORT/COVER]`
    #     filter can be applied in-index over the small post-lane
    #     post-window set)
    #
    # The auto-router without a lane filter still benefits — Mongo
    # can use the prefix `(lane, ingest_ts)` when `lane` is omitted
    # by iterating the two lane buckets separately (fast IXSCAN
    # rather than COLLSCAN).
    await _safe_create_index(
        db.shared_intents,
        [("lane", 1), ("ingest_ts", -1), ("action", 1)],
        deadline_s=heavy_deadline_s,
        name="shared_intents_lane_ingest_action_idx",
    )
    # 2026-02-19 (P1 prod hotfix — operator dashboard NetworkTimeout):
    # The Intents page + Phase Map filter `shared_intents` by
    # `symbol` alone (no `stack`, no `lane`). The 2026-06-XX
    # `(stack, symbol, ingest_ts)` compound above is leading-on-
    # `stack`, so the mongo planner CAN'T use it when the operator
    # filters by symbol without also filtering by stack — which is
    # the default operator flow on the symbol-drill-down panel.
    # Query fell back to COLLSCAN on a multi-million-row
    # collection → NetworkTimeout at the 15s socket cap.
    #
    # `(symbol, ingest_ts)` is the surgical fix: symbol equality
    # uses the index prefix, `ingest_ts -1` covers the descending
    # sort. Bounded IXSCAN, no in-memory sort. Uses
    # `_safe_create_index` because the initial build on prod's
    # ~5M rows would otherwise blow the 6s deadline default.
    await _safe_create_index(
        db.shared_intents,
        [("symbol", 1), ("ingest_ts", -1)],
        deadline_s=heavy_deadline_s,
        name="shared_intents_symbol_ingest_idx",
    )
    await db.shared_brain_opinions.create_index([("runtime", 1), ("topic", 1), ("posted_at", -1)])
    # 2026-07-11 doctrine step 4: per-market-event idempotency.
    # Sparse so existing 114k docs without the field don't
    # conflict; sparse+unique means "enforce uniqueness only on
    # docs where `decision_fingerprint` exists". The same brain
    # evaluating the same symbol against the same completed bar
    # can produce at most ONE opinion. Cooldown becomes secondary
    # — this is the doctrine-level guard against 472-identical-
    # intent cascades regardless of upstream feeder health.
    await _safe_create_index(
        db.shared_brain_opinions,
        [("decision_fingerprint", 1)],
        name="shared_brain_opinions_decision_fp_unique",
        unique=True,
        sparse=True,
    )
    # 2026-07-11 doctrine step 5: consensus dedup at the
    # position layer. Same shape as step 4 one level up —
    # sparse unique index on `consensus_fingerprint`. One
    # {symbol, side, engaged-brain-set} consensus per market
    # event; duplicate transitions silently no-op via
    # DuplicateKeyError. Closes the pattern where four brains
    # agreeing on same NVDA @ same bar_close would create four
    # separate position_ids all landing in consensus_long.
    await _safe_create_index(
        db.shared_positions,
        [("consensus_fingerprint", 1)],
        name="shared_positions_consensus_fp_unique",
        unique=True,
        sparse=True,
    )
    await db.shared_brain_outcomes.create_index([("opinion_id", 1), ("resolved_at", -1)])
    # 2026-02-28 — MC Shelly noise cleanup companion. On preview the
    # collection had grown to 1.86M rows / 528 MB (data + indexes)
    # because Shelly was writing every intent_ingested and every
    # sidecar packet. After the record()-side allowlist filter,
    # this TTL sweeps the historical bulk over 90 days without
    # touching the daily .jsonl file archive (which stays as the
    # long-term training substrate). Chosen conservatively — 90d
    # is enough for outcome-resolver joins on ~14d bracket windows
    # plus plenty of headroom for operator review.
    try:
        await db.mc_shelly.create_index(
            [("ts", 1)],
            name="mc_shelly_ts_ttl_90d",
            expireAfterSeconds=90 * 24 * 3600,
        )
    except Exception:  # noqa: BLE001
        # TTL index may exist with different `expireAfterSeconds`.
        # Mongo doesn't allow mutating TTL in place — operator can
        # drop and recreate via a maintenance script if needed.
        pass
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

    # ── External Signal Intake v1 (2026-02-23) ────────────────────
    # `external_signals` is the witness layer (Pine / TradeLens / MTR).
    # Idempotency: `dedup_key` is unique — TradingView retries cannot
    # double-write. Diagnostics tile queries by `received_at` DESC
    # (recent witnesses) and by `(symbol, received_at)` (per-symbol).
    #
    # `partialFilterExpression` on `dedup_key: {$type: "string"}` —
    # not every witness writer stamps a dedup_key today (e.g., resolver
    # scratch rows in tests); a non-partial unique index would reject
    # any second such doc with dedup_key=null. Uniqueness is still
    # strictly enforced for the string values that DO get set.
    await db.external_signals.create_index(
        "dedup_key",
        unique=True,
        name="external_signals_dedup_unique",
        partialFilterExpression={"dedup_key": {"$type": "string"}},
    )
    await db.external_signals.create_index(
        [("received_at", -1)], name="external_signals_recent_idx",
    )
    await db.external_signals.create_index(
        [("symbol", 1), ("received_at", -1)],
        name="external_signals_symbol_recent_idx",
    )
    await db.external_signals.create_index(
        [("source", 1), ("received_at", -1)],
        name="external_signals_source_recent_idx",
    )

    # ── Verifier-owned credibility ledger (2026-02-23) ────────────
    # One doc per witness source. Verifier updates after observed
    # outcomes. The webhook only $setOnInsert's a fresh UNTRUSTED
    # row on first sight; never mutates existing rows.
    await db.external_source_credibility.create_index(
        "source", unique=True, name="external_source_credibility_unique",
    )
    await db.external_source_credibility.create_index(
        [("status", 1), ("updated_at", -1)],
        name="external_source_credibility_status_idx",
    )

    # ── Manipulation alerts (2026-02-23) ──────────────────────────
    # RoadGuard's witness-cluster detector. Log-only in v1.
    await db.external_signal_manipulation_alerts.create_index(
        [("created_at", -1)],
        name="external_signal_manipulation_alerts_recent_idx",
    )
    await db.external_signal_manipulation_alerts.create_index(
        [("source", 1), ("trigger_type", 1), ("created_at", -1)],
        name="external_signal_manipulation_alerts_lookup_idx",
    )

    # ── 2026-02-25 admin-tile diagnostic 500 sweep ────────────────────
    # Five collections powering admin diagnostic tiles had zero or
    # incomplete index coverage. At prod volumes (~100k+ docs each)
    # their default queries blocked-sort in memory → Mongo socket
    # timeout → operator's diagnostic UI flickered HTTP 500 across
    # ~8 different tiles. Surgical compound indexes that match each
    # tile's actual query shape:

    # `doctrine_sidecars` (2 tiles: Paradox v3 Rollout +
    # Per-Brain Execution Style Profile). Both filter by
    # `intent_version="v3"` AND `outcome_join: {$exists: true}` and
    # then iterate the entire cursor. With no indexes, that's a
    # full-collection scan on every poll (10s interval). Compound
    # index makes both filters indexed.
    await db.doctrine_sidecars.create_index(
        [("intent_version", 1), ("outcome_join", 1)],
        name="doctrine_sidecars_v3_outcome_idx",
    )

    # `pipeline_receipts` Seat Stage Drops tile sorts by ts DESC.
    # The existing `ts_1` index is ascending — Mongo won't use it
    # for a DESC sort without reverse-scan, which it does support
    # but adds cost. An explicit DESC index makes it free. Also
    # add a compound (lane, ts) for the lane-filtered variant.
    await db.pipeline_receipts.create_index(
        [("ts", -1)], name="pipeline_receipts_ts_desc_idx",
    )
    await db.pipeline_receipts.create_index(
        [("lane", 1), ("ts", -1)],
        name="pipeline_receipts_lane_ts_idx",
    )

    # `shared_indicator_snapshots` per-symbol latest lookup
    # (Brain Input Health tile): `find_one({symbol: X},
    # sort=[(computed_at, -1)])`. The existing
    # `source_1_symbol_1_tf_1` index isn't leading-on-symbol+sort.
    await db.shared_indicator_snapshots.create_index(
        [("symbol", 1), ("computed_at", -1)],
        name="shared_indicator_snapshots_symbol_recent_idx",
    )

    # `market_data_key_fetches` Brain Health tile reads
    # `.find().sort(ts, -1).limit(500)`. No prior index.
    await db.market_data_key_fetches.create_index(
        [("ts", -1)],
        name="market_data_key_fetches_recent_idx",
    )

    # `sidecar_checkin_audit` powers the Native Brain Runtimes /
    # Brain Outages tiles. Reads `.find({brain_id, ts$gte}).sort(ts, 1)`.
    # No prior index. Compound (brain_id, ts) covers both filter and
    # sort in one scan.
    await db.sidecar_checkin_audit.create_index(
        [("brain_id", 1), ("ts", 1)],
        name="sidecar_checkin_audit_brain_ts_idx",
    )
    await db.sidecar_checkin_audit.create_index(
        [("ts", -1)],
        name="sidecar_checkin_audit_ts_desc_idx",
    )

    # `brain_metrics_snapshots` polled by the Brain Metrics tile
    # for the 72h timeseries: `.find({captured_at >= cutoff}).sort(captured_at, 1)`.
    await db.brain_metrics_snapshots.create_index(
        [("captured_at", 1)],
        name="brain_metrics_snapshots_captured_idx",
    )

    # `shared_gate_results` powers the Trade Readiness / Equity Dry-Run
    # Autopsy / Direct-Execute Recent tiles. The new direct-execute
    # admin endpoints (2026-02-26) query
    # `.find({kind in [...], ts >= since}).sort(ts, -1)`. Without this
    # compound index the prod collection (years of audit rows) does a
    # COLLSCAN and the endpoint times out with NetworkTimeout against
    # the Mongo Atlas shard.
    #
    # USES `_safe_create_index` with `heavy_deadline_s`.
    await _safe_create_index(
        db.shared_gate_results,
        [("kind", 1), ("ts", -1)],
        deadline_s=heavy_deadline_s,
        name="shared_gate_results_kind_ts_idx",
    )
    await _safe_create_index(
        db.shared_gate_results,
        [("intent_id", 1), ("ts", -1)],
        deadline_s=heavy_deadline_s,
        name="shared_gate_results_intent_ts_idx",
    )

    # ── 2026-02-27 architectural reduction collections ────────────
    # `executions` — one row per (intent → broker attempt). Hot reads:
    #   • `recent()` sorts by ts DESC
    #   • daily-spend aggregation matches `ts ~ "^YYYY-MM-DD"`
    # Wrapped in `_safe_create_index` so a slow Atlas index build
    # CANNOT hang the pod startup. Without this, the auto-router
    # task starts but its first tick blocks forever — symptom: the
    # diagnostics tile shows `tick_count=0 · last_tick_ts=None ·
    # last_tick_error=None` and no trades fire.
    await _safe_create_index(
        db.executions, [("ts", -1)],
        deadline_s=heavy_deadline_s, name="executions_ts_desc_idx",
    )
    await _safe_create_index(
        db.executions, [("intent_id", 1)],
        deadline_s=heavy_deadline_s, name="executions_intent_idx",
    )
    await _safe_create_index(
        db.executions, [("lane", 1), ("ts", -1)],
        deadline_s=heavy_deadline_s, name="executions_lane_ts_idx",
    )
    await _safe_create_index(
        db.executions, [("ok", 1), ("ts", -1)],
        deadline_s=heavy_deadline_s, name="executions_ok_ts_idx",
    )

    # Symbol Registry (2026-07-15, iter-30 P3). Sort-by-updated_at
    # supports the diagnostic admin surface + future daily
    # revalidator. The registry is small (bounded by universe size,
    # ~50 rows at most on today's setup) so we don't need per-broker
    # indexes yet.
    await _safe_create_index(
        db.symbol_registry, [("updated_at", -1)],
        deadline_s=heavy_deadline_s, name="symbol_registry_updated_at",
    )

    # Universe refresh reports (2026-07-15, iter-30 P4) — read
    # pattern: newest-first per lane. TTL 30d so we don't grow
    # unbounded; ~4 refreshes/hour × 2 lanes × 30d ~= 5800 rows.
    await _safe_create_index(
        db.universe_refresh_reports, [("lane", 1), ("refreshed_at", -1)],
        deadline_s=heavy_deadline_s, name="universe_refresh_reports_lane_at",
    )
    await _safe_create_index(
        db.universe_refresh_reports, [("refreshed_at", 1)],
        deadline_s=heavy_deadline_s, name="universe_refresh_reports_ttl",
        expireAfterSeconds=30 * 86400,
    )

    pass
