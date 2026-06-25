"""Regression lint: no new `$stack` groupings or `{"stack": <input>}`
filters in aggregation paths.

Doctrine (2026-02-23 dual-field migration): the canonical identity
field is `stack_canonical`. The raw `stack` field is retained for
forensic audit display ONLY. Any new aggregation, in-memory
grouping, or input-keyed query that uses `stack` instead of
`stack_canonical` will silently re-introduce the legacy/canonical
split bug (e.g. dashboards showing "barracuda" + "camaro" as two
distinct brains).

This test scans the routes/ and shared/ directories for the
forbidden patterns and fails with an actionable message if any
appear OUTSIDE the allow-listed files. The allow-list contains
only files that legitimately project `stack` for display.

If you genuinely need to add a new `$stack` reference, add the
file to `ALLOWED_FILES` with a comment explaining why it's a
display-only projection rather than a grouping/filter.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


BACKEND_ROOT = Path("/app/backend")
SCAN_DIRS = (
    BACKEND_ROOT / "routes",
    BACKEND_ROOT / "shared",
)

# Files allowed to reference `stack` (without `_canonical`) — these
# are display-only projections, NOT groupings/filters. Operator
# intentionally surfaces the legacy form for forensic audit (the
# `stack` column on the post-mortem panel etc.).
ALLOWED_FILES: set[str] = {
    # Display projections — operator wants to see the raw historical
    # `stack` label alongside the canonical resolution.
    "routes/admin_intents_post_mortem.py",
    "routes/admin_paradox_v3.py",
    "routes/admin_brain_metrics.py",
    "routes/intent_inspect.py",
    "routes/admin_intents_funnel.py",  # projection only; group uses stack_canonical
    "routes/scorecard_by_brain.py",    # surfaces stack as historical column
    "routes/intent_origin.py",         # latest_directional payload retains stack

    # Write paths — emission/audit stamps `stack` AND `stack_canonical`.
    "shared/intents.py",
    "shared/intent_bridge_factory.py",
    "shared/chevelle_crypto_intent_bridge.py",
    "shared/redeye_crypto_intent_bridge.py",
    "shared/strategies/canary_runner.py",  # stamps stack_canonical sibling

    # Identity machinery itself — the normalizer, the legend, the
    # auto-submit policy that prefers stack_canonical with a stack
    # fallback for external callers.
    "shared/brain_legend.py",
    "shared/auto_submit_policy.py",
    "shared/execution.py",
    "shared/council.py",                # _brain_id_variants legend-aware
    "shared/brain_metrics.py",          # prefers stack_canonical with fallback
    "shared/brain_doctrine.py",         # STACK_TO_BRAIN_ID legend source-of-truth

    # Routes that don't touch grouping/filtering of intent.stack on
    # the migrated shared_intents collection — they query OTHER
    # collections (positions, brackets, doctrine sidecars, execution
    # receipts, opinions, sovereign audit, …) that have their own
    # write-time stamping pattern. These collections may eventually
    # get their own dual-field migration, but they're out of scope
    # for the 2026-02-23 shared_intents work.
    "routes/brain_emission_diagnose.py",  # shared_intents fully migrated
    "routes/brain_runtime.py",            # shared_intents fully migrated
    "routes/sidecar_diagnostics.py",      # shared_intents fully migrated
    "routes/intent_summary.py",           # shared_intents fully migrated
    "shared/hypothesis.py",               # shared_intents fully migrated
    "shared/diagnostics.py",              # shared_intents fully migrated

    # OTHER-COLLECTION read/write sites (positions, brackets, sidecars,
    # execution receipts, opinion store) — out of scope for the
    # shared_intents-only dual-field migration. Add a per-collection
    # migration if these become dashboard sources of confusion.
    "routes/admin_brackets.py",
    "routes/outcome_join_admin.py",
    "shared/broker/webull_brackets.py",
    "shared/doctrine/shadow_outcome.py",
    "shared/live_positions.py",
    "shared/vrl.py",
    "shared/promotion_artifact_report.py",      # EXECUTION_RECEIPTS collection
    "shared/brains/brain_performance_store.py", # DOCTRINE_SIDECARS collection
    "shared/personalities_routes.py",            # personality dict return value
    "shared/doctrine_routes.py",                 # per_brain_decision_log writes
}


def _scan_for_forbidden(file_path: Path) -> list[str]:
    """Return a list of human-readable violations found in `file_path`.

    Forbidden patterns:
      * `"$stack"` inside an aggregation `$group` / `$push` payload
      * `{"stack": <var>}` where the value is a variable (filter on
        input identity — should be `stack_canonical`)
    """
    violations: list[str] = []
    src = file_path.read_text(encoding="utf-8")
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()
        # Skip comments.
        if stripped.startswith("#"):
            continue
        # Pattern 1: literal "$stack" anywhere (used in $group _id /
        # $push payloads). The legitimate display use of `stack` in
        # projections is `"stack": 1` (NOT prefixed with $).
        if re.search(r'["\']\$stack["\']', line):
            violations.append(
                f"  L{lineno}: literal $stack reference — "
                f"groupings/payloads must use $stack_canonical:\n"
                f"      {stripped}",
            )
        # Pattern 2: `{"stack": var}` filter (i.e. NOT a number for
        # projection). A safe display projection looks like
        # `"stack": 1`. A grouping/filter looks like `"stack": brain`
        # or `"stack": foo.lower()`.
        m = re.search(r'["\']stack["\']\s*:\s*([A-Za-z_])', line)
        if m and not re.search(r'["\']stack["\']\s*:\s*"[a-z_]+"', line):
            # Heuristic: a value starting with a letter/underscore
            # is a variable (`brain`, `body.stack`, etc.). A value
            # that's a string literal like `"camino"` is fine for
            # write-stamping in tests/seeds.
            value_token = m.group(1)
            # Allow string-literal values (start with quote).
            if line.find(f'"stack": "') == -1 and \
               line.find(f"'stack': '") == -1:
                violations.append(
                    f"  L{lineno}: `stack: <var>` filter — input-"
                    f"keyed queries must use `stack_canonical`:\n"
                    f"      {stripped}",
                )
    return violations


def test_no_new_dollar_stack_groupings_or_input_filters():
    """Phase C regression guard — fail loudly if anyone reintroduces
    a $stack grouping or input-keyed stack filter in routes/shared.
    """
    all_violations: dict[str, list[str]] = {}
    for scan_dir in SCAN_DIRS:
        for py_file in scan_dir.rglob("*.py"):
            if py_file.name.startswith("__"):
                continue
            rel_path = py_file.relative_to(BACKEND_ROOT).as_posix()
            if rel_path in ALLOWED_FILES:
                continue
            violations = _scan_for_forbidden(py_file)
            if violations:
                all_violations[rel_path] = violations

    if all_violations:
        msg_lines = [
            "Phase C regression — found forbidden `stack` references "
            "outside the allow-list. The canonical identity is "
            "`stack_canonical` (2026-02-23 dual-field migration).",
            "",
            "Either:",
            "  (a) switch the reference to `stack_canonical`, OR",
            "  (b) if it's a display projection (i.e. `\"stack\": 1`), "
            "add the file to ALLOWED_FILES in this test with a comment "
            "explaining why.",
            "",
            "Violations:",
        ]
        for fname, viols in sorted(all_violations.items()):
            msg_lines.append(f"\n{fname}:")
            msg_lines.extend(viols)
        pytest.fail("\n".join(msg_lines))


def test_allowed_files_actually_exist():
    """The allow-list isn't allowed to drift either — every entry
    must point to a real file in the repo so this regression test
    can't be silently invalidated by a refactor."""
    missing: list[str] = []
    for rel_path in ALLOWED_FILES:
        if not (BACKEND_ROOT / rel_path).is_file():
            missing.append(rel_path)
    assert not missing, (
        f"ALLOWED_FILES contains entries that no longer exist: {missing}. "
        f"If you renamed/deleted these files, update ALLOWED_FILES."
    )
