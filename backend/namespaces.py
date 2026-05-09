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
