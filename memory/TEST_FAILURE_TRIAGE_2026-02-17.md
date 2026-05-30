# Pre-Existing Test Failure Triage (2026-02-17)

**Snapshot:** 41 failures on `main` after pass #29.

**Method:** Ran full suite with `--tb=line` to harvest one-line error
text per failure, grouped by root cause. NONE of these are
regressions from the current session — all pre-existed on `main`
(verified via `git stash` round-trip in pass #29).

This document is the inventory operator asked for. It does NOT fix
anything by itself; each cluster's "Trivial fix" cell is the
operator's call to greenlight before I touch them.

---

## Cluster 1 — `opponent` → `auditor` rename drift  (13 tests, biggest cluster)

**Root cause:** 2026-05-27 doctrine update merged the `opponent`
seat into `auditor` (same brain, two time windows: pre-trade
adversarial argument + post-trade review). The alias-rewrite layer
(`_LEGACY_ROLE_REWRITES["opponent"] = "auditor"`) handles legacy
reads, but the tests below were authored BEFORE the merge and
assert the old `opponent` literal in user-facing payloads.

**Trivial fix:** rename `"opponent"` → `"auditor"` in test
assertions and add a 2-line `# 2026-05-27 rename` comment. ~10 min
for the whole cluster. ZERO behavioral risk.

| Test | Error |
|---|---|
| `test_quorum_and_provenance::test_fresh_position_has_all_required_seats_missing` | `assert ... 'opponent', ...` |
| `test_quorum_and_provenance::test_opponent_silent_flags_adversarial_blindness` | `assert ['opponent', ...] == ['opponent']` |
| `test_quorum_and_provenance::test_governor_silent_flags_governance_blindness` | `assert True is False` |
| `test_quorum_and_provenance::test_full_quorum_clears_all_flags` | `assert True is False` |
| `test_roster::test_assign_none_vacates_role` | `KeyError: 'opponent'` |
| `test_roster::test_default_matrix` | `KeyError: 'opponent'` |
| `test_roster::test_tenure_response_shape` | `missing seat in tenure: opponent` |
| `test_seat_aliases::test_normalize_seat_alias_table_minimum_shape` | `{auditor, ...} == {executor, ..., opponent}` |
| `test_seat_aliases::test_normalize_seat_rewrites_deprecated` | `assert 'auditor' == 'opponent'` |
| `test_seat_aliases::test_normalize_seat_passes_canonical_unchanged` | `assert 'auditor' == 'opponent'` |
| `test_seat_policy_and_auto::test_matrix_returns` | `missing equity seat: opponent` |
| `test_discussion_layer::test_operator_view_includes_all_brains` | `alpha must not claim execution` (downstream of opponent/auditor reshuffle) |
| `test_doctrine_intent_attachment::test_equity_with_empty_snapshot_still_returns_packet` | `assert True is False` (per-seat keys changed) |

---

## Cluster 2 — `get_executor_holder` function removed  (6 tests)

**Root cause:** A previous refactor renamed/inlined
`shared.execution.get_executor_holder`. Tests import it directly
and get `AttributeError`.

**Trivial fix:** First grep for what replaced it (likely
`shared.roster.get_roster()`-based lookup or a method on a class).
Update the import + call. ~15 min.

| Test | Error |
|---|---|
| `test_execution_gates::test_per_order_cap_blocks_above_threshold` | `AttributeError: ... get_executor_holder` |
| `test_execution_gates::test_gate_chain_blocks_when_executor_seat_empty` | same |
| `test_execution_gates::test_gate_chain_passes_when_everything_aligned` | same |
| `test_execution_gates::test_gate_chain_blocks_when_broker_disconnected` | same |
| `test_execution_gates::test_gate_chain_blocks_when_daily_cap_would_be_breached` | same |
| `test_execution_gates::test_hold_action_not_routable` | same |
| `test_execution_gates::test_stale_seat_blocks_after_rotation` | same |

---

## Cluster 3 — Broker bypass-block hardened  (2 tests)

**Root cause:** `shared/broker/alpaca.py` now REJECTS direct
`submit_market_order` calls that lack an MC-minted execution receipt
(doctrine: every broker write must carry a signed receipt from
`broker_router`). This is correct production behavior — the tests
were probing the broker module directly without going through the
router.

**Trivial fix:** Attach a fake-but-shape-correct receipt in the test
setup, OR move these tests to call through `broker_router` instead.
~10 min. **Conceptually a "test should use the public API" cleanup,
not a bug.**

| Test | Error |
|---|---|
| `test_alpaca_broker::test_submit_market_order_requires_exactly_one_of_qty_notional` | `BypassBlocked: no MC execution receipt attached` |
| `test_alpaca_broker::test_submit_market_order_uses_notional_when_supplied` | `BypassBlocked: no MC execution receipt attached` |

---

## Cluster 4 — `doctrine_scorecard()` signature changed  (3 tests)

**Root cause:** The `doctrine_scorecard` function's signature
changed — tests still pass a `stack=` kwarg that's no longer
accepted. (`TypeError: unexpected keyword argument 'stack'`)

**Trivial fix:** Look up new signature, update the 3 callsites in
the test. ~5 min.

| Test | Error |
|---|---|
| `test_doctrine_outcome_join_and_scorecard::test_scorecard_quality_bands_aggregate` | `TypeError: unexpected keyword argument 'stack'` |
| `test_doctrine_outcome_join_and_scorecard::test_scorecard_promotion_blocked_below_min_samples` | same |
| `test_doctrine_outcome_join_and_scorecard::test_scorecard_per_seat_loss_rates` | same |

---

## Cluster 5 — Public Phase 2 Chat endpoints changed  (8 tests)

**Root cause:** The public chat endpoint route or contract changed.
Tests expect `403 Forbidden` (or `422 ValidationError`) but get
`404 Not Found`, AND the response shape dropped `session_id`.

**Risk:** UNCLEAR. Three possibilities, each requires investigation:
  a) Endpoint was intentionally removed (chat tier-gating retired)
  b) Endpoint was moved to a new path
  c) Endpoint was renamed and tier-check now returns 404 instead of 403

**NOT a trivial fix.** Needs ~20 min of forensics on
`/api/public/chat/*` route definitions to know which of (a/b/c) it
is. Recommend deferring until someone needs Chat functionality.

| Test | Error |
|---|---|
| `test_public_phase2::test_chat_refused_for_free` | `404 == 403` |
| `test_public_phase2::test_chat_refused_for_starter` | same |
| `test_public_phase2::test_chat_refused_for_pro` | same |
| `test_public_phase2::test_chat_continues_session` | `404 == 200` |
| `test_public_phase2::test_chat_history_endpoint` | `KeyError: 'session_id'` |
| `test_public_phase2::test_chat_history_refused_for_lower_tier` | `404 == 403` |
| `test_public_phase2::test_chat_history_delete` | `KeyError: 'session_id'` |
| `test_public_phase2::test_chat_validates_input` | `404 == 422` |

---

## Cluster 6 — Sovereign doctrine no longer rejecting  (2 tests)

**Root cause:** Tests assert that submitting `live_trading_enabled=true`
or `mode=PRD with training_signal` should produce HTTP 422 (rejection),
but the endpoint returns 200. The sovereign-mode guard was DEFANGED
in a prior pass per Doctrine (c) — `mode` is a declaration, not a
permission; MC observes, the execution gate decides.

**Trivial fix:** Update the assertions to expect 200 + verify the
declaration is logged but not honored as authority. The TEST is
testing an outdated authority model. ~5 min.

| Test | Error |
|---|---|
| `test_sovereign::test_live_trading_enabled_true_rejected` | `200 == 422` |
| `test_sovereign::test_prd_with_training_signal_rejected` | `200 == 422` |

---

## Cluster 7 — Standalone drift  (7 tests, mixed root causes)

Each requires individual forensics. Listed shortest-fix-first.

| Test | Error | Likely cause | Fix path |
|---|---|---|---|
| `test_no_duplicate_execution_gates::test_no_duplicate_execution_gate_logic` | False-positive on `may_execute = True` in opinion-silence-watchdog TEST | Static scan caught a string literal in a test, not a duplicate gate | Add the watchdog test to the scanner's exclusion list. 2 min. |
| `test_confidence_floor_sweep::test_dampener_drop_never_negative` | `negative dampener_drop at floor 0.2: -1` | Off-by-one in dampener floor logic OR test fixture | Investigate `confidence_floor` module. 15 min. |
| `test_alpaca_execution_pipeline::test_dry_run_cap_per_order_breach` | `'modulate' == 'block'` | Cap-breach action changed from `block` to `modulate` (graceful degrade?) | Update assertion + verify modulate is intended. 5 min. |
| `test_ibkr::test_get_active_none_when_missing` | Async loop attachment error | pytest-asyncio fixture scope drift | Fix test fixture decorator. 5 min. |
| `test_positions::test_all_four_brains_can_post` | `200 == 422` | Position POST validation changed | Update payload shape. 10 min. |
| `test_risk_monitor_and_policy::test_brain_lane_policy_full_cycle` | `404 == 200` | Endpoint renamed/moved | Find new path. 10 min. |
| `test_roster::test_eligibility::test_default_matrix` | `KeyError` (clustered with #1 above; also default matrix shape) | Default matrix gained/lost a key | Update expected shape. 5 min. |

---

## Summary

| Cluster | # tests | Effort | Risk | Recommended? |
|---|---|---|---|---|
| 1 — `opponent` → `auditor` rename | 13 | ~10 min | None | ✅ YES |
| 2 — `get_executor_holder` removed | 6 | ~15 min | None | ✅ YES |
| 3 — Broker bypass-block hardened | 2 | ~10 min | None | ✅ YES (use public API) |
| 4 — `doctrine_scorecard` signature | 3 | ~5 min | None | ✅ YES |
| 5 — Public Phase 2 Chat endpoints | 8 | ~20 min + investigation | Medium (might be intentionally retired) | ⚠️ HOLD — need to know if Chat is intentionally retired |
| 6 — Sovereign doctrine defang | 2 | ~5 min | None | ✅ YES |
| 7 — Standalone drift | 7 | ~50 min total | Mixed | 🟡 Triage individually |
| **Total trivial fixes** | **26** | **~45 min** | None | If you say go |

**Headline number if I clear clusters 1-4 + 6 + the easy ones in 7 (no-duplicate-gates + sovereign-defang):**
- 41 failures → **~10 failures** (cluster 5 chat + remaining cluster 7 stragglers)
- Green-bar rises from 1481/1522 (97.3%) → **~1512/1522 (99.3%)**

Cluster 5 (Chat) is the only one with real risk because it might
mean the endpoint was intentionally removed and the tests should
just be deleted. Operator should confirm.
