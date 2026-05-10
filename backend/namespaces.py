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

# Authority + promotion (governed role evolution)
SHARED_AUTHORITY_STATE = "shared_authority_state"           # one doc per runtime, history embedded
SHARED_PROMOTION_ARTIFACTS = "shared_promotion_artifacts"   # Patent G evidence emitted by runtimes
SHARED_PROMOTION_PROPOSALS = "shared_promotion_proposals"   # pending operator countersign

# Cross-brain discussion layer (mediated; pull-only on consumers)
SHARED_OPINIONS = "shared_brain_opinions"                   # threaded discussion across brains

RUNTIMES = ("alpha", "camaro", "chevelle")

# Advisors are not on the trading ladder. They speak (post opinions) but
# never appear in RUNTIMES — granting them ladder authority would violate
# their advisor-only role. REDEYE reports to Camaro; never bypasses it.
ADVISORS: tuple[str, ...] = ("redeye",)

# Convenience set of every brain that may participate in the discussion layer.
DISCUSSION_PARTICIPANTS: tuple[str, ...] = RUNTIMES + ADVISORS

# ───────────────────────────────────────────────────────────────────────
# RUNTIME ROLES — the FUNCTIONAL kind of brain. Fixed.
#   "Only Alpha has hands. Camaro has teeth. Chevelle has the keys."
# ───────────────────────────────────────────────────────────────────────
ROLES: dict[str, dict] = {
    "alpha": {
        "role": "trader",
        "title": "Trader",
        "tagline": "has hands",
        "description": (
            "Generates executable signals. The only stack whose authority can "
            "ever climb to CO_TRADER or PRIMARY. Must still pass RoadGuard / "
            "Envelope / Patent J gates."
        ),
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
            "Authority can climb the ladder via PromotionArtifact + Patent J + operator."
        ),
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
            "verification, promotion control. Off-ladder — does not trade and "
            "is not promotable to a trading authority."
        ),
        "allowed_actions": [
            "readiness_gate", "calibration_gate", "audit_verify",
            "promotion_decision", "authority_call",
        ],
    },
    "redeye": {
        "role": "advisor",
        "title": "Short-Side Advisor",
        "tagline": "reports to Camaro",
        "description": (
            "Bearish/short-side adversarial scout. Off-ladder. Sends advice "
            "to Camaro; cannot execute, cannot override Alpha. Speaks via the "
            "shared discussion layer; reads peer opinions but never modifies "
            "their state."
        ),
        "allowed_actions": [
            "post_opinion", "read_opinions", "read_roles_manifest",
            "short_advisory", "alpha_alignment_hint",
        ],
    },
}

# ───────────────────────────────────────────────────────────────────────
# AUTHORITY LADDER — what a runtime is currently ALLOWED to do.
# Evolves only via governed promotion (Patent G evidence + Patent J gate
# + operator countersign). Never flipped organically.
# ───────────────────────────────────────────────────────────────────────
AUTHORITY_LADDER: list[str] = [
    "observer",     # 0 — watches only, no recommendations
    "challenger",   # 1 — can recommend veto / reduce / watch
    "advisor",      # 2 — can influence sizing
    "co_trader",    # 3 — can propose executable trades
    "primary",      # 4 — can become execution leader
]
AUTHORITY_LEVEL: dict[str, int] = {s: i for i, s in enumerate(AUTHORITY_LADDER)}

# Off-ladder state for the Governor role (Chevelle). Cannot be promoted onto
# the trading ladder; cannot ever execute.
GOVERNOR_STATE = "governor"

# Authority states that grant execution authority.
EXECUTION_AUTHORITY_STATES = frozenset({"co_trader", "primary"})

# Default authority per runtime on first boot. Promotion writes new history;
# the default is what we install when the doc is missing.
DEFAULT_AUTHORITY: dict[str, str] = {
    "alpha": "co_trader",   # only stack with execution authority today
    "camaro": "challenger",
    "chevelle": GOVERNOR_STATE,
}

# Patent J readiness thresholds (operator-tunable later).
PROMOTION_THRESHOLDS = {
    "ece_max": 0.05,                    # Expected Calibration Error
    "brier_max": 0.20,                  # Brier score
    "min_resolved_rows": 100,           # sample size floor
    "min_disagreement_stability": 0.7,  # stability of dissent over the window
    "max_toxic_memory_24h": 5,          # firewall quarantines in 24h
    "max_role_violations_24h": 0,       # zero tolerance
    "heartbeat_max_age_seconds": 300,   # liveness floor
}

# Heartbeat staleness threshold for the dashboard alert. A runtime is "stale"
# if its last_seen is older than this. Tuned so a single missed heartbeat
# (45s loop) is forgiven, but two consecutive misses raise the alarm.
# Visibility-only — does NOT change authority, broker behavior, or receipt enforcement.
HEARTBEAT_STALE_AFTER_SECONDS = 90


def runtime_can_execute_state(authority_state: str) -> bool:
    """Single source of truth: only authority states on the trading ladder
    that have reached co_trader or primary may execute."""
    return authority_state in EXECUTION_AUTHORITY_STATES


def is_on_ladder(authority_state: str) -> bool:
    return authority_state in AUTHORITY_LADDER


def next_authority(current: str) -> str | None:
    """Returns the next authority state up the ladder, or None if at top
    or off-ladder (governor)."""
    if current not in AUTHORITY_LEVEL:
        return None
    idx = AUTHORITY_LEVEL[current]
    if idx + 1 >= len(AUTHORITY_LADDER):
        return None
    return AUTHORITY_LADDER[idx + 1]
