"""Idempotent seeding of demonstration/observation data so the dashboard has signal on first boot."""
import uuid
import random
from datetime import datetime, timezone, timedelta

from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_CALIBRATORS,
    SHARED_FEATURE_BUILDERS, SHARED_ARTIFACTS,
    ALPHA_DECISION_LOG, CAMARO_SHADOW_ROWS, CHEVELLE_MEMORY_LABELS,
)


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


async def seed_all(db) -> None:
    # Feature builders (shared)
    if await db[SHARED_FEATURE_BUILDERS].count_documents({}) == 0:
        await db[SHARED_FEATURE_BUILDERS].insert_many([
            {"name": "rsi_14", "version": "1.2.0", "kind": "indicator", "deterministic": True, "description": "Relative Strength Index, 14-period."},
            {"name": "atr_14", "version": "1.0.4", "kind": "indicator", "deterministic": True, "description": "Average True Range, 14-period."},
            {"name": "regime_zscore", "version": "0.9.1", "kind": "regime", "deterministic": True, "description": "Cross-asset regime z-score."},
            {"name": "session_clock", "version": "1.0.0", "kind": "time", "deterministic": True, "description": "Normalized intra-session clock."},
            {"name": "vol_cluster", "version": "0.7.3", "kind": "volatility", "deterministic": True, "description": "Realized volatility cluster bucket."},
        ])

    # Calibrators (per-runtime, kept side-by-side but isolated)
    if await db[SHARED_CALIBRATORS].count_documents({}) == 0:
        await db[SHARED_CALIBRATORS].insert_many([
            {"runtime": "alpha",    "name": "alpha_isotonic_v3",  "version": "3.1.0", "method": "isotonic",        "fit_at": _ts(60 * 22)},
            {"runtime": "alpha",    "name": "alpha_platt_v1",     "version": "1.4.2", "method": "platt-scaling",   "fit_at": _ts(60 * 14)},
            {"runtime": "camaro",   "name": "camaro_isotonic_v2", "version": "2.0.7", "method": "isotonic",        "fit_at": _ts(60 * 30)},
            {"runtime": "camaro",   "name": "camaro_temp_scale",  "version": "0.3.1", "method": "temperature",     "fit_at": _ts(60 * 6)},
            {"runtime": "chevelle", "name": "chevelle_iso_v1",    "version": "1.1.0", "method": "isotonic",        "fit_at": _ts(60 * 48)},
            {"runtime": "chevelle", "name": "chevelle_platt_v2",  "version": "2.2.0", "method": "platt-scaling",   "fit_at": _ts(60 * 8)},
        ])

    # Artifact inventory (per-runtime, kept SEPARATE)
    if await db[SHARED_ARTIFACTS].count_documents({}) == 0:
        await db[SHARED_ARTIFACTS].insert_many([
            {"runtime": "alpha",    "artifact": "alpha_xgb",       "version": "v0.7.4", "sha": "a1b2c3d4", "registered_at": _ts(60 * 26)},
            {"runtime": "alpha",    "artifact": "alpha_phase6",    "version": "v0.7.4", "sha": "a1b2c3d4", "registered_at": _ts(60 * 26)},
            {"runtime": "camaro",   "artifact": "camaro_lgbm",     "version": "v0.4.2", "sha": "ee11ff22", "registered_at": _ts(60 * 12)},
            {"runtime": "camaro",   "artifact": "camaro_executor", "version": "v0.4.2", "sha": "ee11ff22", "registered_at": _ts(60 * 12)},
            {"runtime": "chevelle", "artifact": "chevelle_tx",     "version": "v0.2.1", "sha": "99aabbcc", "registered_at": _ts(60 * 4)},
            {"runtime": "chevelle", "artifact": "chevelle_authority", "version": "v0.2.1", "sha": "99aabbcc", "registered_at": _ts(60 * 4)},
        ])

    # ADL receipts (observed, not executed)
    if await db[SHARED_RECEIPTS].count_documents({}) == 0:
        runtimes = ["alpha", "camaro", "chevelle"]
        actions = ["enter_long", "enter_short", "exit", "scale_in", "hold"]
        symbols = ["ES", "NQ", "CL", "GC", "ZB"]
        rng = random.Random(7)
        bulk = []
        for i in range(45):
            rt = runtimes[i % 3]
            bulk.append({
                "id": str(uuid.uuid4()),
                "runtime": rt,
                "action": rng.choice(actions),
                "intent": {
                    "symbol": rng.choice(symbols),
                    "qty": rng.choice([1, 2, 3]),
                    "confidence": round(rng.uniform(0.45, 0.92), 3),
                },
                "observed": True,
                "executed": False,
                "timestamp": _ts(i * 7),
            })
        await db[SHARED_RECEIPTS].insert_many(bulk)

    # Memory labels (firewall log)
    if await db[SHARED_MEMORY].count_documents({}) == 0:
        runtimes = ["alpha", "camaro", "chevelle"]
        labels = ["safe", "review", "quarantine"]
        bulk = []
        rng = random.Random(11)
        for i in range(36):
            rt = runtimes[i % 3]
            lbl = rng.choices(labels, weights=[7, 2, 1])[0]
            bulk.append({
                "id": str(uuid.uuid4()),
                "runtime": rt,
                "label": lbl,
                "reason": {
                    "safe": "passed schema + drift checks",
                    "review": "drift score above warn threshold",
                    "quarantine": "schema mismatch on feature vector",
                }[lbl],
                "payload_summary": f"feature_vector batch #{i}",
                "timestamp": _ts(i * 11),
            })
        await db[SHARED_MEMORY].insert_many(bulk)

    # Per-runtime decision logs (kept ISOLATED)
    if await db[ALPHA_DECISION_LOG].count_documents({}) == 0:
        await db[ALPHA_DECISION_LOG].insert_many([
            {"id": str(uuid.uuid4()), "decision": "phase6_proposal", "score": 0.71, "symbol": "ES", "timestamp": _ts(15)},
            {"id": str(uuid.uuid4()), "decision": "phase6_proposal", "score": 0.62, "symbol": "NQ", "timestamp": _ts(42)},
            {"id": str(uuid.uuid4()), "decision": "phase6_proposal", "score": 0.55, "symbol": "CL", "timestamp": _ts(120)},
        ])
    if await db[CAMARO_SHADOW_ROWS].count_documents({}) == 0:
        await db[CAMARO_SHADOW_ROWS].insert_many([
            {"id": str(uuid.uuid4()), "shadow": "executor_proposed", "side": "long", "size": 2, "symbol": "GC", "timestamp": _ts(9)},
            {"id": str(uuid.uuid4()), "shadow": "executor_proposed", "side": "flat", "size": 0, "symbol": "ES", "timestamp": _ts(33)},
        ])
    if await db[CHEVELLE_MEMORY_LABELS].count_documents({}) == 0:
        await db[CHEVELLE_MEMORY_LABELS].insert_many([
            {"id": str(uuid.uuid4()), "authority_call": "abstain", "horizon": "30m", "symbol": "ZB", "timestamp": _ts(6)},
            {"id": str(uuid.uuid4()), "authority_call": "long_bias", "horizon": "60m", "symbol": "NQ", "timestamp": _ts(54)},
        ])
