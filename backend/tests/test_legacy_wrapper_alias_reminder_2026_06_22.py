"""Reminder (non-breaking): the four legacy wrapper aliases were
deliberately kept after the 2026-06-22 rename to bridge the audit-row
retention window. They are READ-ONLY aliases — no new code emits
them — but they MUST stay until every persisted intent referencing
the old wrapper_name string has rotated out of Mongo.

This test is **informational**. It does NOT fail. It only PRINTS a
reminder banner when run after the target review date so the
operator notices the leftover aliases during a routine `pytest -v`
sweep. Removing the aliases prematurely would break the post-mortem
UI for any historical intent whose `evidence.legacy_wrapper.name`
still holds `alpha_legacy_executor` (or the other three).

Why a reminder and not a hard assertion (per operator pin
2026-06-22b):
    > "make it a reminder test, not something that suddenly breaks
    >  production"

Cleanup procedure (when the time comes):
    1. Confirm the post-mortem retention cycle has passed
       `ALIAS_REVIEW_DATE` and no recent audit rows reference the
       old names.
    2. Delete the four alias entries from `WRAPPER_REGISTRY` in
       `shared/legacy_brain_wrappers.py`.
    3. Delete this file.
"""
from __future__ import annotations

import sys
import warnings
from datetime import date, timezone

# After this date, the test PRINTS a reminder banner. It still
# passes — the operator has explicitly asked for a non-breaking
# reminder, not a fail-after-deadline timebomb.
ALIAS_REVIEW_DATE = date(2026, 9, 22)  # 90 days after the rename

LEGACY_ALIAS_KEYS = (
    "alpha_legacy_executor",
    "chevelle_legacy_governor",
    "camaro_legacy_strategist",
    "redeye_legacy_adversary",
)

CANONICAL_KEYS = (
    "alpha_legacy_doctrine",
    "chevelle_legacy_doctrine",
    "camaro_legacy_doctrine",
    "redeye_legacy_doctrine",
)


def test_legacy_aliases_still_present():
    """SAFETY: the four old-name aliases must still resolve to the
    same function as the canonical name. Removing them mid-window
    breaks the post-mortem UI for any audit row still tagged with
    the old `wrapper_name` string.

    This assertion is the PROTECTIVE half of the reminder. It only
    flips when the operator (deliberately) deletes an alias.
    """
    from shared.legacy_brain_wrappers import WRAPPER_REGISTRY

    for old_key, new_key in zip(LEGACY_ALIAS_KEYS, CANONICAL_KEYS):
        assert old_key in WRAPPER_REGISTRY, (
            f"Legacy alias {old_key!r} missing from WRAPPER_REGISTRY. "
            f"This breaks the post-mortem UI for any historical intent "
            f"whose evidence.legacy_wrapper.name still holds {old_key!r}. "
            f"If you intentionally retired the alias, ALSO delete the "
            f"test file `test_legacy_wrapper_alias_reminder_2026_06_22.py`."
        )
        assert WRAPPER_REGISTRY[old_key] is WRAPPER_REGISTRY[new_key], (
            f"Alias {old_key!r} no longer points to the canonical "
            f"function {new_key!r}. Old audit rows would now resolve to "
            f"the wrong wrapper — operator-visible drift in the "
            f"post-mortem UI."
        )


def test_alias_cleanup_reminder():
    """INFORMATIONAL: emit a visible reminder when the review date
    has passed. This test ALWAYS passes — it just prints a banner so
    the operator notices the aliases during their routine `pytest`
    sweep and can decide whether to retire them.
    """
    from datetime import datetime
    today = datetime.now(tz=timezone.utc).date()
    days_past_review = (today - ALIAS_REVIEW_DATE).days

    if days_past_review < 0:
        # Pre-review window — silent pass.
        return

    banner = (
        "\n"
        "  ┌─────────────────────────────────────────────────────────┐\n"
        "  │  REMINDER: legacy_brain_wrappers backward-compat aliases  │\n"
        f"  │  passed review date {ALIAS_REVIEW_DATE.isoformat()} "
        f"({days_past_review}d ago).            │\n"
        "  │  If audit-row retention has cycled, you may now retire:  │\n"
        "  │    alpha_legacy_executor                                 │\n"
        "  │    chevelle_legacy_governor                              │\n"
        "  │    camaro_legacy_strategist                              │\n"
        "  │    redeye_legacy_adversary                               │\n"
        "  │  See test_legacy_wrapper_alias_reminder_2026_06_22.py    │\n"
        "  │  for the cleanup procedure.                              │\n"
        "  └─────────────────────────────────────────────────────────┘"
    )
    print(banner, file=sys.stderr)
    warnings.warn(
        f"legacy_brain_wrappers aliases past review date by "
        f"{days_past_review}d — see test docstring for cleanup",
        UserWarning,
        stacklevel=2,
    )
    # The test still PASSES. Always.
    assert True
