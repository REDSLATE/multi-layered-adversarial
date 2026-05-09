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
