"""Operator-tunable rollup constants. Env-overridable so the operator
can tighten/relax windows without redeploy."""
from __future__ import annotations

import os


# Window before a row is eligible for rollup.
ROLLUP_WINDOW_DAYS: int = int(
    os.environ.get("ROLLUP_WINDOW_DAYS", "60"),
)

# After a row is rolled up, the verbose original lives this many more
# days before being purged. Lets the operator revert a bad rollup.
ROLLUP_DELETE_HOLD_DAYS: int = int(
    os.environ.get("ROLLUP_DELETE_HOLD_DAYS", "7"),
)

ROLLUP_VERSION: str = "v1"


# ── doctrine guards ──
# A row carrying ANY of these key:value pairs is NEVER rolled up. These
# are real-money / live audit rows that need verbatim preservation.
PROTECTED_FLAGS: dict = {
    "executed": True,
    "live_order": True,
    "real_money": True,
}

# A row whose `label` / `labels` / `memory_label` field intersects this
# set is NEVER rolled up. Quarantines are firewall — collapsing them
# breaks training-data integrity.
PROTECTED_LABELS: set = {
    "quarantine",
}

# Collections that are NEVER rolled up. Shellys + brain memories live
# here. Operator can expand at any time.
PROTECTED_COLLECTIONS: set = {
    # MC
    "mc_shelly",
    "shared_labeled_memories",
    "brain_memories",
    "brain_memories_dead",

    # Per-brain shellys / memories (not yet present in MC's DB but
    # registered defensively so future moves don't accidentally roll
    # them up).
    "alpha_shelly",
    "alpha_brain_memories",
    "camaro_shelly",
    "camaro_brain_memories",
    "chevelle_shelly",
    "chevelle_brain_memories",
    "redeye_shelly",
    "redeye_brain_memories",
}
