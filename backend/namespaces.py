"""Centralized collection naming. Importing from here is the only sanctioned way.
Crossing namespaces (e.g. alpha reading camaro_shadow_rows) is a doctrine violation."""

# Shared infrastructure (the "shared nervous system")
SHARED_RECEIPTS = "shared_adl_receipts"
SHARED_MEMORY = "shared_labeled_memories"
SHARED_CALIBRATORS = "shared_calibrators"
SHARED_FEATURE_BUILDERS = "shared_feature_builders"
SHARED_ARTIFACTS = "shared_artifact_inventory"

# Per-runtime decision authorities (the three separate brains)
ALPHA_DECISION_LOG = "alpha_decision_log"
CAMARO_SHADOW_ROWS = "camaro_shadow_rows"
CHEVELLE_MEMORY_LABELS = "chevelle_memory_labels"

# Heartbeats from runtime sidecars (one upserted doc per runtime)
SHARED_HEARTBEATS = "shared_heartbeats"

RUNTIMES = ("alpha", "camaro", "chevelle")

# ───────────────────────────────────────────────────────────────────────
# RUNTIME ROLES — adversarial design, server-enforced.
#   "Only Alpha has hands. Camaro has teeth. Chevelle has the keys."
# Execution authority is welded to runtime IDENTITY at the ingest layer.
# A leaked Camaro or Chevelle token cannot ever cause a trade, even in
# live mode. Receipts that violate the role are recorded with
# role_violation=true so operators see misbehavior immediately.
# ───────────────────────────────────────────────────────────────────────
ROLES: dict[str, dict] = {
    "alpha": {
        "role": "trader",
        "title": "Trader",
        "tagline": "has hands",
        "description": (
            "Generates executable signals. Only stack eligible for live/paper "
            "execution. Must still pass RoadGuard / Envelope / Patent J gates."
        ),
        "execution_allowed": True,
        "allowed_actions": [
            "enter_long", "enter_short", "exit", "scale_in", "hold",
            "phase6_proposal",
        ],
    },
    "camaro": {
        "role": "challenger",
        "title": "Challenger",
        "tagline": "has teeth",
        "description": (
            "Shadows Alpha. Attacks Alpha's thesis. Logs counterfactuals. "
            "Can recommend veto / reduce / watch. Cannot place trades."
        ),
        "execution_allowed": False,
        "allowed_actions": [
            "shadow_proposal", "counterfactual", "veto", "reduce", "watch",
            "executor_proposed",
        ],
    },
    "chevelle": {
        "role": "governor",
        "title": "Governor",
        "tagline": "has the keys",
        "description": (
            "Memory firewall, readiness gate, calibration gate, audit "
            "verification, promotion control. Cannot place trades."
        ),
        "execution_allowed": False,
        "allowed_actions": [
            "readiness_gate", "calibration_gate", "audit_verify",
            "promotion_decision", "authority_call",
        ],
    },
}


def runtime_can_execute(runtime: str) -> bool:
    """Single source of truth: only the Trader role may ever execute."""
    return ROLES.get(runtime, {}).get("execution_allowed", False)
