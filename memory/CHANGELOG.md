## 2026-05-26 (pass #7) — Single-Sign Promotion (B1, hard convert)

Operator confirmed: solo-operator deployment, dual-sign is security theater. Removed entirely.

**Code changes (`shared/promotion.py`):**
- Module docstring updated to reflect the new doctrine.
- `propose_from_latest_artifact`: `required_signatures = 1` for every tier (was `2 if primary else 1`).
- `countersign`: dropped the `awaiting_second_sign` parking path and the "same operator cannot sign twice" 409. One countersign → immediate elevation regardless of tier.
- Status flow simplified to `pending → approved | rejected`.

**What's preserved:**
- Readiness gate (Patent J) — still required to PASS. Failed readiness → 412 with no signing allowed. The doctrine collapse only relaxed the human bar; the technical bar stands.
- Audit chain — signer email, timestamp, note all still recorded. Authority state history still appended on elevation.
- Admin auth — still required for the endpoint.
- Reject endpoint — unchanged.

**Back-compat:** Any legacy proposal sitting in `awaiting_second_sign` from before the change (mid-flight at deploy time) will finalize on the next single countersign. Both signers preserved in the audit trail.

**Tripwires rewritten:** `tests/test_dual_sign_promotion.py` (filename retained for archaeology — anyone reading git history sees "we used to have dual-sign here, then collapsed it on 2026-05-26"). 5 tests, all passing:
1. Primary tier single-sign elevates immediately (was the prohibited path)
2. Failed readiness still blocks (412) — doctrine guard intact
3. Non-primary single-sign elevates (unchanged behavior)
4. Propose endpoint always sets `required_signatures=1`
5. Legacy `awaiting_second_sign` rows finalize on one more sign (back-compat)

**Operator playbook (when ready to promote Alpha on prod):**
```
TOKEN=<your prod admin token>

# See pending proposals
curl -H "Authorization: Bearer $TOKEN" \
  https://mission.risedual.ai/api/admin/promotion/proposals?status=pending

# Confirm readiness passes
curl -H "Authorization: Bearer $TOKEN" \
  https://mission.risedual.ai/api/admin/promotion/readiness/alpha

# Countersign — one click, you're done
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"note":"alpha → primary"}' \
  https://mission.risedual.ai/api/admin/promotion/<proposal_id>/countersign
```

---


## 2026-05-26 (pass #6) — Live Trading Enablement: Sizing Gate + Kill Switch

Operator confirmed: ready to enable execution. Camaro to take crypto seat (operator's call when ready). Kraken live (crypto) + Alpaca paper (equity). Phase 2 broker bridge already exists (`shared/broker_router.route_order`) — what was missing was the operator's safety rails. Built both.

### #4 Sizing Gate — `shared/sizing_gate.py` (NEW)

Phase 4 Ladder Doctrine. Hard per-order cap that **overrides every other sizing input** when enabled.

**Env vars:**
- `MICRO_LIVE_ENABLED=true|false` (default false)
- `MICRO_LIVE_DEFAULT_CAP_USD=5.0`
- `MICRO_LIVE_CRYPTO_CAP_USD=5.0` (per-lane override)
- `MICRO_LIVE_EQUITY_CAP_USD=5.0`

**Doctrine:** Evaluates BOTH the engineering lane cap (`exposure_caps.cap_for_lane` — $500 crypto, $100k equity) AND the operator's micro_live rail. **Tighter rail wins.** Fail-CLOSED to 0 on garbage / negative / non-numeric inputs.

**Provenance:** Every clamped order carries `sizing_provenance` on its receipt with the requested USD, final USD, binding rail, both cap values, and the micro_live state. Operator can trace exactly which rail bound the size.

### #5 Kill Switch — `routes/trading_controls.py` (NEW)

Mongo-backed runtime switch. Auto-router consults it on every tick. Operator flips via HTTP (no redeploy).

**Endpoints:**
- `GET /api/admin/trading/status` — read-only state (runtime flag, env flag, will_fire computed, micro_live mode, last toggle audit fields)
- `POST /api/admin/trading/toggle` — `{enabled: bool, reason: str}` — flips the switch. **Enabling REQUIRES a non-empty reason** (audit-chain receipt). Disabling does not.
- `GET /api/admin/trading/audit?limit=N` — append-only audit log of every flip

**Doctrine:** **Fail-CLOSED.** First boot returns `enabled=false`. Mongo unreachable → `is_trading_enabled` returns False. Two layers must align: env `AUTO_ROUTER_ENABLED=true` AND runtime `trading_controls.enabled=true`. Either OFF = no orders.

**Halt is non-destructive:** existing positions stay open, broker reconciliation keeps running, gates still evaluate. Only `route_order()` is suppressed.

### Auto-router wired (`shared/auto_router.py`)

`_route_one()` now does (in order):
1. Phase 1: **Sizing Gate** — `evaluate_sizing(requested, lane)` returns clamped notional + provenance
2. Phase 1b: **Runtime Kill Switch** — `is_trading_enabled()` check
3. Phase 2-6: existing gate chain → broker route → receipt → audit (unchanged)

Receipt now carries `sizing_provenance` for audit.

### Verified live (5/5 smoke tests pass):
1. ✅ `GET /status` baseline: `trading_will_fire=false` (fail-CLOSED first boot)
2. ✅ Enable without reason → 400 "reason required when enabling trading"
3. ✅ Enable with reason → 200, audit row written by admin@risedual.io
4. ✅ Disable → 200, second audit row written
5. ✅ `GET /audit` returns both flips in reverse-chrono order

**Tripwires (13 new, all passing):**
`tests/test_sizing_gate_and_kill_switch.py` — sizing gate: lane cap binds when micro_live off, micro_live clamps when on, per-lane overrides work, tighter-rail-wins doctrine (both directions), invalid input fail-CLOSED. Kill switch: first-boot disabled, fail-CLOSED on unset state, set/read/audit roundtrip, disable-after-enable.

### Operator playbook (when ready to trade)

```
# 1. Confirm kill switch is OFF (default)
curl … /api/admin/trading/status

# 2. Set micro_live env in prod, redeploy
MICRO_LIVE_ENABLED=true
MICRO_LIVE_DEFAULT_CAP_USD=5

# 3. Move Camaro into crypto seat (or whichever brain/lane you want)
curl -X POST … /api/admin/roster/assign \
     -d '{"role":"crypto","brain":"camaro"}'

# 4. FLIP THE SWITCH
curl -X POST … /api/admin/trading/toggle \
     -d '{"enabled":true,"reason":"first live crypto session — micro_live $5"}'

# 5. Watch /api/admin/trading/audit + Kraken account for fills.
# 6. To halt instantly:
curl -X POST … /api/admin/trading/toggle \
     -d '{"enabled":false,"reason":"halting for review"}'
# Takes effect within AUTO_ROUTER_INTERVAL_SEC (default 30s).
```

---


## 2026-05-26 (pass #5) — Governor-Exclusivity Doctrine

Operator pinned the seat-eligibility doctrine to one rule:

> **All seats are open to all brains EXCEPT `governor` (and its crypto twin `crypto_governor`), which are EXCLUSIVE to Chevelle and RedEye.**

**Implementation in `shared/roster.py`:**
- New doctrine constants: `_GOVERNOR_EXCLUSIVE_SEATS = ("governor", "crypto_governor")` and `_GOVERNOR_EXCLUSIVE_BRAINS = ("chevelle", "redeye")`.
- `DEFAULT_ELIGIBILITY` rebuilt via `_build_default_eligibility()`: every cell True except governor cells for alpha/camaro (False).
- `_ensure_assignment_eligible()` now refuses governor → alpha/camaro BEFORE consulting the stored matrix (defense-in-depth against stale or corrupted matrix docs). Vacating (`brain=None`) is always allowed.
- `POST /eligibility` endpoint refuses any attempt to set `allowed=True` for a governor seat on alpha or camaro. Operator can still tighten cells; cannot loosen governor.
- Docstring at top of file rewritten to reflect new doctrine.

**Stored matrix migrated:** ran live update on preview MongoDB — `alpha.governor`, `alpha.crypto_governor`, `camaro.governor`, `camaro.crypto_governor` all flipped True → False. Stamped `updated_by="doctrine_migration_2026_05_26"`.

**Live smoke-tested (all expected outcomes confirmed):**
1. `POST /eligibility` `{brain:"alpha", role:"governor", allowed:true}` → **400** "exclusive to chevelle, redeye"
2. `POST /assign` `{role:"governor", brain:"camaro"}` → **400** "camaro cannot occupy it"
3. `POST /assign` `{role:"governor", brain:"redeye"}` → **200** assignment.governor=redeye
4. `POST /assign` `{role:"governor", brain:"chevelle"}` → **200** restored to chevelle

**Tripwires (33 passing, 0 regressions):**
- New: `tests/test_governor_exclusivity_doctrine.py` (13 tests) — DEFAULT_ELIGIBILITY shape, _GOVERNOR_EXCLUSIVE_* constants, assignment validator rejects alpha/camaro for governor, accepts chevelle/redeye, vacate-always-allowed, non-governor seats unaffected.
- Updated: `tests/test_roster.py::TestEligibility` (3 tests rewritten to express new doctrine — old tests asserted the now-superseded "all seats open to all brains" rule).

**Operator note (Camaro execution):** the doctrine guard only locks the *governor* seat. Camaro **is fully eligible for executor, strategist, auditor, opponent, advisor, crypto, and every other crypto_* seat**. If you want Camaro to execute trades, swap Camaro into `executor` (equity) or `crypto` (crypto) — both are now one POST away with no doctrine obstacle.

---


## 2026-05-26 (pass #4) — Preview-Bleed-to-Prod Audit + Fixes

User asked me to check the preview for anything that might have been pushed unintentionally to production. Three real findings, all fixed.

**Fix #1: Login.jsx — admin email no longer pre-filled**
`frontend/src/pages/Login.jsx` line 9: `useState("admin@risedual.io")` → `useState("")`. Admin email was being shipped pre-populated on the login form (dev convenience that leaked to prod). Now the field shows the placeholder hint only. Verified live via screenshot.

**Fix #2: `mc_memory/` + `test_reports/iteration_*.json` untracked from git**
`backend/mc_memory/*.jsonl` files were tracked at 23 MB and growing daily — operational telemetry, not source. Added to `.gitignore`; ran `git rm --cached -r backend/mc_memory/` + `git rm --cached test_reports/iteration_*.json`. Files preserved on disk (so MC keeps writing); just no longer tracked. **Tracked repo size dropped from ~29 MB → 6 MB.**

This is likely the root cause of the user's intermittent "Save to GitHub" failures — 23 MB of bloat made every push fragile under Cloudflare/edge timeouts.

**Fix #3: CORS env-driven origin pinning**
`backend/server.py` lines 405-411: previously hardcoded `allow_origins=["*"]`. Now reads `CORS_ALLOWED_ORIGINS` env var (comma-separated). When set: exact-match origins + `allow_credentials=True`. When unset: falls back to wildcard (preview/local-dev backward compat). Production should set `CORS_ALLOWED_ORIGINS=https://mission.risedual.ai`.

**Smoke-tested:** backend healthy, CORS headers honoring env default (wildcard, no env set in preview), login page renders with empty email field.

**Things audited and confirmed CLEAN:**
- `.env` files gitignored (~40 entries in `.gitignore`) — preview URLs cannot leak via GitHub
- No `console.log` / `debugger` / `debug=True` in shipping code
- No hardcoded `localhost:8001` URLs in production paths (only in tests + env-var fallbacks)
- `mc_memory/` content scanned — no secrets / tokens / private keys
- `test_credentials.md` is gitignored ✓

**Operator note:** the JWT `_create_access` issues a 60-minute access token + 7-day refresh. Cookies are scoped per-host so preview cookies cannot validate on production (or vice versa) — that's correct isolation.

---


## 2026-05-26 (pass #3) — Spread-bps Enrichment + Sovereign TTL→Rollup

**Fix #1: `spread_bps` MC-side enrichment (Camaro crypto + equity)**

Camaro was shipping empty `doctrine_snapshot` dicts, triggering RoadGuard's `ROADGUARD_MISSING_SPREAD_BPS` kill on every intent. MC now walks a fallback ladder at ingest before the gate chain runs:
1. `brain` — brain-supplied `snapshot.spread_bps` (if numeric, non-sentinel)
2. `mc_derived_bid_ask` — canonical `compute_spread_bps(bid, ask)` if both present
3. `mc_indicator_cache` — most recent `shared_indicator_snapshots` row (configurable freshness window, default 10 min)
4. `mc_kraken_public` — Kraken public Ticker API (crypto only, **opt-in** via `SPREAD_FETCH_KRAKEN_ENABLED=true`)
5. `sentinel_unknown` — `SPREAD_BPS_UNKNOWN=9999.0` so RoadGuard fails closed with explicit provenance

Provenance stamped on every intent: `snapshot.spread_source` + `spread_enrichment_diagnostics.attempts`. Operator can audit MC's reasoning at any time.

**Verified live (3 ingest cases):**
- `bid=99.5, ask=100.5` (no spread) → `mc_derived_bid_ask` → 100 bps ✅
- `{}` empty crypto snapshot → walks ladder → `sentinel_unknown` 9999.0 ✅
- `spread_bps=7.5` brain-supplied → preserved → `source=brain` ✅

Wired into both runtime path (`/api/intents`) and admin proxy (`/api/admin/intents`).

**Files:** new `shared/market_data/__init__.py` + `shared/market_data/spread_enrichment.py`, updated `shared/intents.py` (both ingest paths).

---

**Fix #2: `sovereign_state_history` TTL→rollup conversion**

Previous 30d TTL-DELETE index `sovereign_history_ttl_30d` removed by `scripts/drop_sovereign_history_ttl.py`. Replaced with `storage_rollup` pipeline (60d window, 7d hold), preserving labels instead of deleting.

**New derivation in `shared/storage_rollup/derive.py`:**
- Sovereign-row detection via signature `mode + learning_rate + brain`
- `derive_movement` → `"snapshot"` (not a trade)
- `derive_event` → `delta_clamped_pos|neg|zero` / `delta_applied_pos|neg` / `no_change`

**Slim rollup keeps** (sovereign-specific): `mode`, `confidence_delta`, `raw_confidence_delta`, `delta_was_clamped`, `learning_rate`, `posted_as`, `seat_epoch`. **Drops** the heavy fields: `weights`, `recent_outcomes`, `notes`.

Registered in `shared/storage_rollup/registry.py` with `ts_field="received_at_dt"`. Now picked up by `/api/admin/storage-rollup/preview` and `/run`.

**Tripwires added (24, all passing):**
`tests/test_spread_enrichment_and_sovereign_rollup.py` covers: brain-supplied wins, brain-sentinel falls through, derive from bid/ask, indicator cache fresh, indicator cache stale ignored, sentinel when no source, diagnostics carry attempt trail, canonical formula sanity. Sovereign: recognized as snapshot, clamp/apply/no-change events (pos/neg/zero), non-sovereign row not misclassified, rollup doc preserves analytical fields, intent rollup does not carry sovereign fields. Plus TTL drop idempotency.

**Total tripwires across today's work:** 95 (FK schema 10 + modulator bounds 11 + storage tightening 7 + rollups 31 + spread+sovereign 24, plus 12 unchanged cross-brain memory). Zero regressions to existing 1,080 passing tests.

**Operator playbook for sovereign migration:**
```
# 1. Drop the legacy TTL-delete index
python scripts/drop_sovereign_history_ttl.py --dry-run
python scripts/drop_sovereign_history_ttl.py

# 2. Preview rollup impact (will now include sovereign_state_history)
curl … /api/admin/storage-rollup/preview

# 3. Run rollup when ready
curl -X POST … /api/admin/storage-rollup/run
```

---


## 2026-05-26 (storage pass #2) — Cold Rollups (60-day Compaction)

Operator handoff merged. Past 60 days, verbose telemetry collapses to slim `{movement, event}`-labeled rollup rows. Nothing leaves Mongo. Shellys + brain_memories + quarantine labels + executed real-money trades are doctrine-protected.

**New module: `shared/storage_rollup/`**
- `config.py` — `ROLLUP_WINDOW_DAYS=60`, `ROLLUP_DELETE_HOLD_DAYS=7`, `PROTECTED_FLAGS={executed,live_order,real_money}`, `PROTECTED_LABELS={quarantine}`, 12 `PROTECTED_COLLECTIONS` (mc_shelly, shared_labeled_memories, brain_memories, per-brain shellys, per-brain brain_memories).
- `derive.py` — movement (long/short/flat/blocked/rejected/ambiguous) + event (executed_win/executed_loss/blocked_<gate>/rejected_at_ingest/shadow_observation/ambiguous). Reads existing fields only — never guesses; ambiguous rows are skipped.
- `registry.py` — 17 collections + per-collection `ts_field` map (MC uses `ingest_ts`, `ts`, `timestamp`, `resolved_at` — not hardcoded `created_at`).
- `runner.py` — two-phase pipeline:
  - **Phase 1 (rollup):** insert slim row to `{collection}_rollups`, stamp original with `rolled_up_at`. Idempotent (re-runs find nothing new).
  - **Phase 2 (purge):** delete original after `ROLLUP_DELETE_HOLD_DAYS` post-rollup. Safety net refuses to delete if the slim rollup doc is missing.

**Endpoints (admin JWT only):**
- `GET  /api/admin/storage-rollup/preview` — Phase 1 dry-run
- `POST /api/admin/storage-rollup/run` — Phase 1 live
- `GET  /api/admin/storage-rollup/purge-preview` — Phase 2 dry-run
- `POST /api/admin/storage-rollup/purge` — Phase 2 live
- `GET  /api/admin/storage-rollup/stats` — per-collection sizes + rollup coverage

**Tripwires added (31, all passing):**
`test_storage_rollup.py` covers: BUY/OPEN→long, SHORT→short, SELL/HOLD/CLOSE→flat, blocked-gate carries name, executed-win/loss/scratch events; protected flags (executed/live_order/real_money); protected labels (quarantine); 12 protected collections by name; old rejected row rolls correctly; executed row NEVER rolls; protected collection skipped at runner; idempotent re-run picks zero; recent row untouched; purge protects collection; purge refuses orphan rows; purge deletes after hold; dry-run writes nothing.

**Verified live on preview backend:**
- `/preview` returns 4 MC collections scanned (0 rolled — no rows >60d in preview env), 13 brain-runtime collections correctly tagged `collection_not_present_in_mc`.
- `/stats` shows: shared_intents 8.4k docs 26 MB, doctrine_sidecars 7.5k docs 19 MB, shared_adl_receipts 16.5k 6 MB, shared_brain_outcomes 0.5k <1 MB, all 0% rolled (clean baseline).

**Operator playbook on prod:**
```
curl … /api/admin/storage-rollup/stats        # baseline
curl … /api/admin/storage-rollup/preview      # impact estimate (dry-run)
curl -X POST … /api/admin/storage-rollup/run  # Phase 1 — slim rollups written
# wait ≥7 days, verify nothing flagged
curl … /api/admin/storage-rollup/purge-preview  # Phase 2 dry-run
curl -X POST … /api/admin/storage-rollup/purge  # Phase 2 live — originals deleted
```

---


## 2026-05-26 (later same day) — Storage Tightening Pass #1

**Camaro identified as storage criminal — 65% of all brain-attributed writes.**
- `shared_intents`: Camaro 8,373 of 8,406 (99.6%)
- `mc_shelly`: Camaro 25,046 of 37,615 (66.6%)
- `doctrine_sidecars`: Camaro 7,265 of 7,448 (97.5%)
- `sovereign_state_history`: Camaro 2,840 of 4,194 (67.7%)

Of Camaro's 8,373 intents, 4% (338) were `rejected_at_ingest` muted-by-brain-lane-policy rows at ~879 B each. 96% are real intents at ~4,100 B each (the doctrine_packet/snapshot/weights bloat — bigger lever, future work).

**P0-2 (storage): Slim rejection rows (`shared/intents.py::_audit_lane_policy_rejection`)**
- Stripped `evidence`, full `rationale`, `executed_at`, `execution_receipt_id` from the row.
- Truncated rationale to 240-char `rationale_stub` (full text preserved in mc_shelly).
- Added `slim_v=2` marker so future regressions are catchable.
- Result: rejection row size drops from ~880 B → <500 B (verified by tripwire `test_rejection_size_under_one_kb`).
- Downstream consumers untouched: `confidence_floor_sweep` already skips `rejected_at_ingest`; `brain_emission_diagnose` only needs `gate_state` + counts which are preserved.

**P0-3 (storage): 30-day TTL on `sovereign_state_history`**
- Writer (`shared/sovereign_mode_guard.py`) now stamps `received_at_dt` as a BSON Date alongside the ISO string `received_at` (TTL requires Date type).
- TTL index installed in `db.py::ensure_indexes`: `received_at_dt → expireAfterSeconds=30*86400`. Idempotent install.
- Backfill: `scripts/backfill_sovereign_history_ttl.py` walks legacy rows, parses ISO `received_at`/`ts`, falls back to `ObjectId.generation_time`, stamps the Date field. Verified end-to-end: 4,197/4,197 rows now have the field.

**Tripwires added (7 new tests):** `tests/test_storage_tightening_2026_05_26.py`
- Rejection row contract (no heavy fields, slim_v marker, downstream fields preserved).
- Rejection row size budget (<1 KB).
- TTL index installed at startup (30d on `received_at_dt`).
- New history writes carry BSON Date (not ISO string).
- Backfill idempotent / writes from ISO / dry-run safe.

**Total tripwires passing across all today's work:** 40 (this pass + earlier schema work). Pre-existing 33 unrelated failures unchanged.

**Surfaced for follow-up:**
- The bigger Camaro lever is on **normal intents** (8,035 of them at 4.1 KB each ≈ 33 MB just in preview). The `doctrine_packet` + `snapshot` + `evidence.regime_fp` payloads bloat each row. Splitting `shared_intents` into a lean core + sidecar `intent_packets` keyed by `intent_id` is the proposed next move.
- Index-to-data ratio is 63% in preview — likely worse on prod; warrants an audit.

---


## 2026-05-26 — Memory Firewall Schema Tightening + Modulator Bound Enforcement

Operator priority: data needs labeling and control. Schema only.

**P0-1: shared_labeled_memories.memory_id FK**
- `MemoryLabelIn` (`shared/ingest.py`) now accepts top-level `memory_id` + `decision_id` (both optional for back-compat). Both persisted on `shared_labeled_memories` row.
- `runtime_cross_brain_memories._quarantined_memory_ids` upgraded: PRIMARY direct FK lookup, REGEX fallback only for legacy rows with no FK. Both paths union into one quarantine set. The two paths can run in parallel forever; once corpus is fully migrated, regex fallback is deletable.
- Backfill: `scripts/backfill_memory_label_fk.py` — idempotent, dry-run flag, regex-parses legacy `payload_summary`/`reason` and stamps the top-level FK. Safe to re-run.
- New endpoint `GET /api/runtime/quarantined-memory-ids` — clean handshake for brain-side memory modulators to fetch the current quarantine set (30s cache).

**P0-2: MC-side modulator bound enforcement**
- `IntentIn.memory_modulator` (new optional field): brain-supplied receipt. Pydantic validator REJECTS any `value` outside [-0.25, +0.10] with 422 (no silent clamping — buggy brains must surface).
- Accepts legacy `modulator` alias and normalizes to canonical `value`. 4 KB payload cap (anti-smuggling).
- `post_intent` flow: when brain ships a receipt, MC trusts the brain's already-modulated `confidence`, stamps the receipt with `source=brain` + `mc_validated=true` + `mc_bounds`, and SKIPS its own server-side compute (no double-application). When brain omits the receipt, the legacy MC-side compute still runs and now ALSO excludes quarantined memory_ids from its similarity pool.
- `shared/memory_modulator.compute_memory_modulator` now fetches the quarantine set first (fail-CLOSED if it can't reach the firewall) and excludes those memory_ids from the Mongo query plus a second-pass filter on `decision_id` for belt-and-suspenders.

**Tripwires added (33 new tests, all passing):**
- `tests/test_memory_label_fk_schema.py` (10 tests): schema accepts FK; back-compat preserved; DB round-trip; direct FK quarantine lookup; regex fallback for legacy rows; union of FK + legacy paths; backfill idempotency; backfill writes legacy rows; dry-run is a no-op.
- `tests/test_memory_modulator_bounds.py` (11 tests): bounds inclusive at -0.25/+0.10; out-of-bound rejected both directions; legacy `modulator` alias accepted; missing/non-numeric `value` rejected; receipt optional; 4 KB cap; non-dict rejected.
- All 13 existing `test_cross_brain_memories.py` tripwires still pass.

**Verification:** end-to-end smoke confirmed via direct `_quarantined_memory_ids` call against MongoDB. Backend restarts clean.

**Pre-existing failures (33 tests, unrelated, confirmed via git stash):** `test_execution_gates`, `test_quorum_and_provenance`, `test_public_phase2`, `test_sovereign`, etc. Untouched by this PR.

---


## 2026-05-24 (cont'd) — Cross-Brain Memory Join (`/api/runtime/memories`)

### Shipped — the Shellys are linked

`GET /api/runtime/memories?symbol=AAPL&lane=equity&limit=50` — runtime-token authed, returns memories from ALL 4 brains for a given symbol, source-tagged and source-weighted.

### Doctrine guarantees (tripwire-enforced)

**Quarantine contagion**
If ANY brain files a `quarantine` label for a memory_id, that memory is excluded from the `peer_memories` view corpus-wide. One brain saying "don't train on this" kills it everywhere. The quarantined corpus is still inspectable via `?include_quarantined=true` for forensics.

The endpoint parses `decision_id=<id>` out of `shared_labeled_memories.reason` and `payload_summary` (regex covers alphanumeric + underscore + hyphen, not just hex — the previous regex would have missed brain-side ID conventions like `WILD-<uuid>`).

**Per-source weighting**
Each brain's safe rows carry `source_weight ∈ [0.5, 2.0]`. Formula: `clamp(0.5, 2.0, 2.0 * win_rate)`, computed from `shared_brain_outcomes` over the last 90 days (env: `MEMORY_LINK_WIN_WINDOW_DAYS`).

  - No data → weight 1.0 (neutral)
  - 50% wins → 1.0
  - 60% wins → 1.2
  - 100% wins → 2.0 (clamped)
  - 0% wins → 0.5 (clamped)

Brains get calibrator-blessed training weights baked into the response — no client-side scoring needed.

### Live verification (preview snapshot)
```
counts_by_brain: alpha=0  camaro=0  chevelle=0  redeye=0  (no AAPL memories on preview yet)
weights_by_brain:
  alpha:    w=137 l=111 win_rate=0.5524 → weight=1.1048
  camaro:   w= 40 l= 60 win_rate=0.40   → weight=0.80
  chevelle: w= 40 l= 40 win_rate=0.50   → weight=1.00
  redeye:   w= 29 l= 28 win_rate=0.5088 → weight=1.0175
```

### Cache
60s server-side per `(symbol, lane, limit, include_quarantined)`. A brain polling on heartbeat hits cache 4-6 times per real query.

### Response shape
```
{symbol, lane, asked_by, cache_hit,
 counts_by_brain: {alpha, camaro, chevelle, redeye},
 weights_by_brain: {brain: {wins, losses, win_rate, source_weight, ...}},
 quarantine_corpus_size,
 peer_memories: [{...row, source_brain, source_weight, quarantined: false}],
 safe_count,
 quarantined_count,
 quarantined_memories: [...]   # only if ?include_quarantined=true
}
```

### Tests
- 13 new tripwires: weight math (5), auth (3), quarantine contagion end-to-end (1), per-brain weights shape (1), counts shape (1), helper resolution (1), boundary clamps (1)
- **Tripwire total: 411 passing** (was 398; +13 net)
- Live verified: 200 + per-brain weight calculation, 401 auth refused

### Files shipped
- `backend/routes/runtime_cross_brain_memories.py` (new)
- `backend/tests/test_cross_brain_memories.py` (new)
- `backend/server.py` (router registration)

### Brain-side usage pattern
```
GET /api/runtime/memories?symbol=AAPL&lane=equity
  X-Runtime-Token: $BRAIN_TOKEN

→ {peer_memories: [
     {memory_id, source_brain: "alpha",    source_weight: 1.10, ...},
     {memory_id, source_brain: "redeye",   source_weight: 1.02, ...},
     {memory_id, source_brain: "camaro",   source_weight: 0.80, ...},
   ], weights_by_brain: {...}, ...}
```

Brain can fold `source_weight` directly into its training loss. A 1.10-weighted Alpha memory contributes 10% more gradient than a neutral one; a 0.80-weighted Camaro memory 20% less. The calibrator's wisdom is baked into the corpus itself.

---


## 2026-05-24 (cont'd) — Opinion Auto-Resolver + OPEN/CLOSE verbs

### Two shipped this turn

#### 1. `shared/opinion_resolver.py` — server-side market-data auto-grader

Closes the 458/485 operator-driven outcomes gap. Background worker
(every 5 min, env-configurable) scans `shared_opinions` for unresolved
DIRECTIONAL stances older than the horizon (default 24h), fetches
current price for the symbol's lane, computes sided PnL, and writes an
outcome to `shared_brain_outcomes` with `resolved_by="auto:market-data"`.

**Doctrine pins (tripwire-enforced):**
- ONLY `long` and `short` stances auto-resolve. `observation`, `endorse`,
  `veto` stay operator/peer-driven (price alone can't grade them).
- Lane-aware win/loss thresholds (crypto ±2%, equity ±1%) — matches the
  existing `observation_resolver`'s scale.
- `long`+price↑=win, `short`+price↓=win (sided PnL).
- No anchor → skip, never poison.
- Idempotent — re-run cannot create duplicate outcomes for same `opinion_id`.

**Anchor capture** added to `shared/opinions.py`: every long/short opinion
now stamps `anchor_price` at post time using the resolver's price fetcher
(best-effort, fails open if price fetch errors).

**Lifecycle**: worker starts in `server.py::lifespan` alongside the
observation resolver. Stops cleanly on shutdown.

**Config (env-overridable):**
- `OPINION_RESOLVER_TICK_SEC` default `300`
- `OPINION_RESOLUTION_HORIZON_HOURS` default `24`

**Tests:** 23 new tripwires covering stance lockdown, lane thresholds,
sided PnL math, horizon respect, no-anchor skip, no-price retry,
end-to-end win/loss/no-event grading for both long and short.

#### 2. `OPEN` / `CLOSE` action verbs on `/api/intents`

Extended `IntentIn.action` Literal to include `OPEN` and `CLOSE` for
symmetry with the lifecycle vocabulary. Translation happens immediately
in `post_intent` so the 12-gate chain only ever sees canonical actions.

- `action="OPEN"` requires `direction: "long"|"short"`; rewrites to
  `BUY` (long) or `SHORT` (short). 422 if direction missing.
- `action="CLOSE"` requires `lane`; delegates to
  `routes.runtime_position_close.close_position()` which discovers
  side+qty from the broker and routes the inverse-side intent through
  the SAME gate chain. 422 if lane missing; 503 if broker disconnected.
- Legacy `BUY`/`SELL`/`SHORT`/`COVER`/`HOLD` unchanged. Brain teams that
  don't want the lifecycle vocabulary can continue using the canonical
  verbs directly.

**Tests:** 10 new tripwires for verb translation (legacy still works,
OPEN with direction, CLOSE with lane, invalid direction rejected,
direction optional for legacy verbs).

### Live verification (preview)
- Opinion resolver started in lifespan logs:
  `opinion_resolver: started tick=300s horizon=24.0h`
- POST `/api/intents` with `action=OPEN` (no direction) → 422 with explicit message
- POST `/api/intents` with `action=CLOSE` (no lane) → 422
- POST `/api/intents` with `action=CLOSE, lane=equity` (preview, Alpaca disconnected) → 503 (cleanly delegated to close_position)

### Tripwire total: **398 passing** (was 365; +33 net)
- 23 opinion_resolver
- 10 intent_open_close_verbs
- 1 pre-existing unrelated failure (`test_runtime_position_discovery.py`)

---


## 2026-05-24 (cont'd) — `/api/runtime/positions/close` shipped

### The gap this closed
Brains could OPEN positions today via `POST /api/intents` with `action=BUY`/`SHORT` — works through the 12 gates. **Closing was the gap**: to close a long, the brain had to (a) know its exact broker position size, (b) pick the right inverse side, (c) compute fractional sizing for partial closes. No brain had clean access to (a). Result on prod: AMZN/GOOGL/MSFT/NVDA positions accumulated 50-90 shares each — every BUY went through, no SELL ever did.

### Endpoint
- `POST /api/runtime/positions/close` — auth via `X-Runtime-Token` (any of 4 brains)
- Body: `{symbol, lane: "equity"|"crypto", fraction: 0<f≤1.0 (default 1.0), rationale?, confidence?}`
- Returns: `{intent_id, closing_brain, symbol, lane, close_action, underlying_qty, close_qty, underlying_side, fraction, routed_through_gate_chain: true}`

### Doctrine guarantees
- **NOT a broker bypass**. The close goes through `shared.intents.post_intent()` — the same 12-gate chain as a normal intent. A lane freeze or any guard blocks the close just like an open.
- Long position → `action=SELL`. Short position → `action=COVER`. No other mapping exists.
- Intent stamped with `close_intent=True, closing_brain, close_fraction, close_underlying_qty, close_target_qty, close_underlying_side` for forensic distinguishing of opens vs. closes in the audit feed.
- 404 when no open position exists. 503 when Alpaca/Kraken disconnected.

### Files
- `backend/routes/runtime_position_close.py` (new)
- `backend/tests/test_runtime_position_close.py` (new — 14 tripwires)
- `backend/server.py` (router registration)

### Tests
- 14 new tripwires: long→SELL, short→COVER, partial close (fraction=0.5), schema (lane enum, fraction bounds), auth (no token, bad token), 404 no-position, 503 disconnected, gate-chain routing verification
- Live curl verified 401 / 422 / 503 paths
- **Tripwire total: 365 passing** (was 351; +14 net). Same pre-existing unrelated failure.

### Brain-side adoption (1-line change per brain)
Instead of the brain trying to construct a SELL intent itself, brain teams replace their open-close bookkeeping with:
```
POST /api/runtime/positions/close
  Header: X-Runtime-Token: $BRAIN_TOKEN
  Body: {"symbol": "AMZN", "lane": "equity"}
→ {intent_id: "...", close_action: "SELL", close_qty: 50.0, ...}
```
MC handles the discovery, side selection, sizing, and gate routing.

---


## 2026-05-24 (cont'd) — `/api/runtime/broker-status` shipped

### Doctrine — 4-tier credential separation pinned

  TIER 0  Public market data (OHLC, ticker)         — no auth, anyone
  TIER 1  Account state derived from private keys   — MC SHARES via /runtime/broker-status
  TIER 2  MC's own records (positions, receipts)    — MC SHARES via /runtime/positions etc.
  TIER 3  Mutating actions (open/close orders)      — Brains REQUEST via /api/intents; MC routes through 12 gates

Keys never leave MC. State derived from keys CAN leave MC.

### Endpoint
- `GET /api/runtime/broker-status` — unified, both lanes in one response
- `GET /api/runtime/broker-status/{lane}` — per-lane variant
- Auth: any valid `X-Runtime-Token` (operator can revoke per-brain by rotating its env token)
- Response identical for all brains — endpoint is read-only state, doesn't care WHO asks
- Server-side cache: 10s TTL per-lane (caps Kraken/Alpaca rate-limit pressure when all 4 brains poll on 30s heartbeats)

### Payload shape (per lane)
```
{lane, connected, execution_enabled, lane_execution_enabled,
 broker_live_order_enabled,
 scopes: {query_funds, trade, ...},                   # bool per permission
 balance_preview: {BTC: "0.001", ...},                # crypto only, top-3 assets
 account_state: {cash, buying_power, daytrade_buying_power,
                 equity, pattern_day_trader, trading_blocked},  # equity only
 public_key_preview: "AKxx…1234",                     # 4-char preview ONLY
 connected_at, updated_at,
 last_fill_at, last_error, last_error_at}
```

### Hard tripwire: NEVER leak full keys
`test_response_never_includes_full_keys` plants a fake key string in the
credentials doc and asserts the endpoint response contains neither the
full public_key nor encrypted_private_key. **Cannot regress accidentally.**

### Tests (12 new tripwires)
- Auth required (unified + per-lane)
- Bogus token rejected
- Bad lane rejected  
- Returns `asked_by` field with matched brain name
- Each of 4 brain tokens unlocks the endpoint
- **Secret-leak tripwire** (above)
- Disconnected shape (crypto + equity)
- Equity account_state populated when connected
- Cache returns same object within TTL
- Cache separates lanes

### Tripwire total: **351 passing** (was 339; +12 net)
- Same pre-existing unrelated failure (`test_runtime_position_discovery.py`)

### How brains should use it
```
status = GET /api/runtime/broker-status
         Header: X-Runtime-Token: $BRAIN_TOKEN

if not status['crypto']['connected']:
    skip_crypto_intent()
elif not status['crypto']['execution_enabled']:
    emit_shadow_only()
elif status['crypto']['balance_preview'] is too small:
    size_down_or_skip()
else:
    emit_intent_normally()
```

Closes the asymmetry where brains POST blind into the void without
knowing if MC is even connected to the broker. Sidecars wire this on
their next deploy.

---


## 2026-05-24 (cont'd) — Learning Scoreboard + new schema-health blocker

### Shipped: `GET /api/admin/learning/scoreboard`
Single endpoint answers operator's 5 truth checks:
- Open positions age buckets + oldest hours
- Closes by reason (`take_profit / stop_loss / trailing_stop / max_hold_time / executor_call / operator_manual / other / unknown`)
- Outcome mix + scratch% + per-brain win rate
- Memory labels by brain (count, last_write_at, silent_hours, silent flag)
- Schema-health warning when `outcome=None` rate is high

File: `backend/routes/learning_scoreboard.py`
Mount: `server.py:336`
No new tests this turn — read-only endpoint, structure verified live.

### 🚨 SCHEMA BLOCKER surfaced by scoreboard probe

Preview MC state:
- **404 governance positions open**, oldest 314 hours (~13 days)
- `shared_positions` (governance store) = 438 rows; states are `proposed / discussing / consensus_long / consensus_short / rejected`
- `shared_live_positions` (broker-fill lifecycle store) = **0 rows**. Position monitor / max_hold guard / TP / SL / trailing-stop appear never to have populated this collection.
- `shared_brain_outcomes` = 485 rows, **100% have `outcome=None`**
- `shared_position_audit` = 904 rows

Implication: **Lifting `MAX_HOLD_MINUTES` and the confidence floor alone may NOT produce graded outcomes.** Two upstream pipelines look broken:
1. **Position lifecycle write path** — broker fills aren't landing in `shared_live_positions`. Either the position monitor doesn't run, doesn't write, writes to a different name, or runs only on prod.
2. **Resolver outcome labeling** — even when outcome rows exist (485 on preview), the `outcome` field is null. Calibrator has nothing to grade.

### Confirmed brain memory labeling silence (preview)
| Brain | Last write | Silent hours |
|---|---|---|
| Alpha    | 2026-05-09 10:00 | 376 (15+ days) |
| Camaro   | 2026-05-09 08:13 | 377 (15+ days) |
| Chevelle | 2026-05-13 17:56 | 272 (11+ days) |
| REDEYE   | never            | n/a            |

All 4 brains stopped between May 9-13. Brain-side regression confirmed (the MC endpoint `/api/ingest/memory-labels` accepts writes — verified earlier with REDEYE wiring).

### Next agent must:
1. Validate scoreboard against **production** MC (preview may have different state than prod — operator confirmed prod has TP/SL/max_hold close events visible in MC Memory Store)
2. **Fix outcome resolver** — find where rows are written to `shared_brain_outcomes` with null `outcome` field, populate the `win/loss/scratch/stopped_out` label correctly
3. **Validate position monitor is writing to `shared_live_positions`** on Prod (preview has zero rows; this may be a preview-only data gap, but needs confirmation)
4. **Then** redeploy + watch scoreboard for 7-10 days

---


## 2026-05-24 — Doctrine course-correction (operator decision)

### Reverted (P0 from prior checkpoint)
- **Brain eligibility hard-lock removed**. Doctrine restored: *"Identity does
  not grant authority. Seat policy does."* All 4 brains × all 12 seats = True
  by default. Operator may tighten specific cells via the eligibility UI.
- **REDEYE no longer seated by default** — opponent vacant. REDEYE lives
  across positions via stances, not in a seat. Operator decides who (if
  anyone) sits in opponent.
- Tests updated: `test_roster.py::TestEligibility` rewritten to assert
  all-True default + that the operator may still narrow per-cell.
- Frontend `BrainOperatorPage.jsx::BRAIN_PROFILE.expected_seats` broadened
  back to all seats for every brain.

### Trading restriction loosening (operator decision)

After 3 months of running with 1.5M intents and ZERO resolved outcomes,
the operator identified `max_hold_time_guard` as the actual learning
bottleneck (every position scratching at 24h before take-profit /
stop-loss / trailing-stop could fire).

**Two knobs changed:**

1. **`MAX_HOLD_MINUTES`: 1440 (24h) → 10080 (7 days)**
   - File: `shared/risk/position_monitor.py:79`
   - Env override: `POSITION_MONITOR_MAX_HOLD_MINUTES`
   - Doctrine: longer hold = positions actually resolve = brains can be
     graded for the first time.

2. **Execution confidence floor: 0.30 → 0.35**
   - File: `shared/auto_router.py` (was hardcoded; now env-controlled)
   - Env override: `RISEDUAL_EXEC_CONFIDENCE_FLOOR`
   - Doctrine: tighten broker-eligible aggression slightly so weak
     opinions stay in shadow until the new outcome data (from the
     max_hold lift) proves they deserve to graduate.
   - `OBSERVATION_MIN_CONFIDENCE = 0.30` unchanged — shadow-only logging
     stays permissive. This is a SHADOW/EXECUTION split: opinions still
     get recorded at 0.30; only orders get routed at 0.35.

**Caps held**:
- `CRYPTO_PER_ORDER_USD = $500` (unchanged)
- `CAP_PER_ORDER_USD = $100k` equity (unchanged; already wide for paper)
- `CAP_PER_DAY_USD = $1M` (unchanged)
- `CAP_OPEN_NOTIONAL_USD = $1M` (unchanged)
- `LANE_SPREAD_CAP` equity 50 bps / crypto 200 bps (unchanged)

**Recheck after 1 week of data**:
- win/loss/scratch mix (currently 100% scratch)
- average hold time (will rise from ~24h cap to ~6-72h organic)
- TP / SL / trailing-stop hit rates
- confidence bucket performance (does 0.30-0.35 perform poorly enough
  to justify keeping it in shadow, or does it earn graduation?)

### Tripwire status: **339 passing** (no regressions from today's work)
- 1 pre-existing unrelated failure (`test_runtime_position_discovery.py`)

---


## 2026-05-24 — Session Checkpoint (operator-driven diagnostic session)

### Shipped this session
- **Shelly Memory Ingest spec-locked** — `POST /api/runtime/shelly/memories` + `POST /api/admin/shelly/memories` matching REDEYE's `MC_MEMORY_INGEST_SPEC.md` verbatim. Enum hard-locks, sign invariants, idempotent on `(brain, memory_id)`, `data_unavailable` quarantine to `brain_memories_dead`. **19 new tripwires.**
- **Assignable RosterPanel mounted** on `/admin/overview` (was orphaned). Operators can now actually assign brains to seats from the UI.
- **Frontend strategist rename** wired through `RosterPanel.jsx`, `BrainOperatorPage.jsx`, legacy `decider` rewritten to `strategist` at ingress.

### ⚠️ CRITICAL — must revert next session
- **Eligibility hard-lock I added VIOLATES DOCTRINE**. Operator explicitly corrected:
  *"The seat bears the restrictions. NOT the brain. ALL brains should be eligible for ALL seats. Only the position (seat policy) restricts what authority the occupant has."*
- Also: **REDEYE should NOT be in any seat by default**. Operator's intent: REDEYE lives across positions via stances, not in a seat. Default opponent assignment was my error.
- **Files to revert**:
  - `backend/shared/roster.py` → `DEFAULT_ELIGIBILITY` back to all-True (24 cells); `DEFAULT_ASSIGNMENTS["opponent"]=None`
  - `backend/tests/test_roster.py::TestEligibility` → drop the hard-lock assertions; assert "all brains × all seats = True"
  - `frontend/src/pages/BrainOperatorPage.jsx::BRAIN_PROFILE.expected_seats` → broaden back to all 6
- Keep: strategist rename, auditor reinstated as real seat, the legacy `decider→strategist` boundary rewrite.

### 🚨 CRITICAL OPERATOR FINDINGS (surfaced via screenshots) — these are the REAL problems

#### Three months of running, ZERO trainable outcomes
- MC Memory Store: **1,526,108 events** logged. 91% gate-pass rate. Looks healthy on the surface.
- `BRAIN TRACK RECORD: NO RESOLVED` — **not a single position has resolved into a trainable outcome.**
- Root cause (suspected): `max_hold_time_guard` is scratching every position before it can hit take-profit or stop-loss. Closed positions tagged `scratch` via `[max_hold_time_guard]`.
- **Next agent priority #1**: diagnose `shared/crypto/max_hold_time.py` + equity equivalent. The hold time is too short OR the take-profit/stop-loss never fire. Without real outcomes, NO BRAIN CAN BE GRADED. Three months wasted.

#### Memory labeling firewall has been silent for 15 days
- `shared_labeled_memories`:
  - Alpha: 13 records, last write **2026-05-09** (15 days silent)
  - Camaro: 12 records, last write **2026-05-09**
  - Chevelle: bulk dump 2026-05-18, then silent
  - REDEYE: **0 records ever** — never wired to the labeling firewall at all
- This pipeline feeds training data. It stopped feeding two weeks ago.
- **Next agent priority #2**: grep `/api/ingest/memory-label` or equivalent endpoint, check write logs, determine if brain-side stopped calling OR MC stopped accepting. Likely brain-side regression but MC may have schema drift.

#### Brain asymmetry — heartbeat ≠ intent emission
- **Camaro/Chevelle**: heartbeats rare, intents flow constantly (1.5M from Camaro alone)
- **Alpha/REDEYE**: heartbeat regular, ~zero intents visible
- Alpha is likely producing `HOLD` verdicts (silent on the wire) — investigate Alpha's decision loop.
- REDEYE having zero intents is **expected** (opponent doesn't initiate) but it also has **zero stances, zero opinions, zero memories** — meaning REDEYE's ENTIRE output surface is dark. Cannot graduate from shadow→live without recorded performance data.
- **Next agent priority #3**: write `/api/admin/runtime-activity-audit` — single endpoint that fans out to `shared_intents`, `runtime_opinions`, `position_stances`, `sovereign_audit_log`, `brain_memories`, `runtime_heartbeats` per runtime; returns counts + last-write timestamps. Gives operator a one-page truth view of "what is each brain actually doing."

#### Kraken bypass — false alarm, but defense gap remains
- 6 BTC trades (May 23-24, ~$75 each, mechanical 6h cadence after a 3-min retry burst) appeared on Kraken dashboard.
- **Pattern matches Kraken's "Recurring Buy" feature, not MC.** MC has no DCA/scheduler code. Operator should check Kraken → Settings → Recurring orders and cancel.
- **Defense gap NOT closed**: MC has zero visibility into Kraken's actual fill stream. Anything that touches the Kraken account outside MC's adapter goes undetected. **Kraken Rogue-Fills Reconciler** (proposed but not built) would poll `TradesHistory` hourly, join against `execution_receipts`, flag unmatched fills as `UNVERIFIED_BROKER_EXECUTION`. **Priority #4** (lower than learning-loop fixes).

### Files referenced (no-touch unless reverting):
- `backend/shared/roster.py` (eligibility lock — revert)
- `backend/shared/seat_policy.py` (strategist policy row — keep)
- `backend/shared/mc_shelly.py` (STR position code — keep)
- `backend/routes/brain_memory_ingest.py` (spec-locked — keep)
- `backend/tests/test_brain_memory_ingest.py` (19 tripwires — keep)
- `frontend/src/components/RosterPanel.jsx` (now mounted — keep, but reconsider after revert)
- `frontend/src/pages/Overview.jsx` (mounts assignable panel — keep)

### Tripwire status
- **339 passing** (was 321 baseline; +18 net)
- 1 pre-existing unrelated failure: `test_runtime_position_discovery.py::test_runtime_list_returns_open_by_default` (seed-fixture issue)

---


## 2026-05-24 — Shelly Memory Ingest (spec-locked, REDEYE-ready)

**Endpoint contract** matches REDEYE's `MC_MEMORY_INGEST_SPEC.md` verbatim.

### Routes (live)
- `POST /api/runtime/shelly/memories` — `X-Runtime-Token` auth (per-brain self-push)
- `POST /api/admin/shelly/memories`   — Admin JWT (operator backfill)
- `GET  /api/admin/brain-memories/summary?brain=…`
- `GET  /api/admin/brain-memories/ingest-audit?brain=…&limit=…`

### Request shape (locked)
```
{batch_id, brain, memories[{
  memory_id, decision_id, symbol, lane, decided_at,
  decision: {raw_action, display_action, confidence, execution_decision},
  resolution: {outcome, realized_r, mae, mfe, entry_price, exit_price, resolved_at, mode},
  features: {…≤20 keys, ≤4KB},
  text_summary: "…≤512 chars"
}]}
```

### Response shape
`{ok, batch_id, brain, received, stored, duplicates, parked_dead, rejected[]}`
- HTTP 207 on partial success (any rejected rows)
- 422 on schema violations (enum/range/bounds)

### Guarantees verified live
- Idempotent on `(brain, memory_id)` — re-POST increments `duplicates`
- `mode="data_unavailable"` quarantined to `brain_memories_dead`
- Enum hard-locks: `raw_action`/`display_action` ∈ {BUY,SELL,HOLD};
  `execution_decision` ∈ {ALLOW,BLOCKED}; `mode` ∈ {shadow,live,data_unavailable};
  `lane` ∈ {crypto,equity,options,futures,fx,unknown}; `outcome` ∈ {-1,0,1}
- Sign invariants: `mae ≤ 0`, `mfe ≥ 0`
- Symbol uppercased at ingress
- HOLD rows accepted with null entry/exit prices + zero r/mae/mfe
- Cross-brain push blocked: a token belonging to brain X cannot post
  memories tagged `brain=Y`
- Bulk cap: ≤500 memories per batch; ≤20 feature keys; ≤4KB features
  payload; ≤512-char text_summary

### Tests (19 new tripwires)
- `test_brain_memory_ingest.py` — full contract coverage
- Tripwire total: **339 passing** (was 321 baseline; +18 new)

### REDEYE-side requirements answered
- Endpoint path: `POST /api/runtime/shelly/memories` ✓
- Token header: `X-Runtime-Token` ✓ (matches existing convention)
- Lane taxonomy: `crypto | equity | options | futures | fx | unknown` ✓
- Features: bounded ≤20 keys / ≤4KB ✓
- Embeddings: MC will regenerate server-side from `text_summary` (REDEYE
  doesn't ship its `shelly_vectors`)
- HOLD rows: accepted by MC (signal-poor individually, useful in aggregate)
- `data_unavailable` rows: stored in `brain_memories_dead`, never counted
  as outcomes
- 429 backpressure: MC has no explicit rate limit yet (REDEYE's
  self-throttle at 10 msg/s is sufficient for the 16k backfill)

### REDEYE-side outstanding
- A preview MC token: use the existing `REDEYE_INGEST_TOKEN` env value
  (see backend `.env`) — same token already used for opinions/heartbeat.

---


## 2026-05-24 — Roster Doctrine v2 (5-seat equity, eligibility hard-lock)

**Operator clarification**: The `decider` seat is renamed to `strategist`. The
auditor seat is reinstated. Seat eligibility is hard-locked per identity.

### Final 5 equity seats
- `strategist` (was `decider`) · `executor` · `auditor` · `governor` · `opponent`
- `advisor` is deprecated (vacant default, no eligibility)

### Eligibility doctrine
| Brain    | strategist | executor | auditor | governor | opponent |
|----------|------------|----------|---------|----------|----------|
| alpha    | ✓          | ✓        | ✓       | ✗        | ✗        |
| camaro   | ✓          | ✓        | ✓       | ✗        | ✗        |
| chevelle | ✗          | ✗        | ✗       | ✓        | ✓        |
| redeye   | ✓          | ✓        | ✓       | ✓        | ✓        |

Crypto lane mirrors the same constraints on parallel seats (`crypto`,
`crypto_strategist`, `crypto_auditor`, `crypto_governor`, `crypto_opponent`).

### Backward compatibility
- `POST /api/admin/roster/assign` (or `/swap`) with `role=decider` is silently
  rewritten to `strategist` (and `crypto_decider` → `crypto_strategist`).
- Legacy DB roster docs are auto-migrated on first read (`get_roster()`).
- `SEAT_ALIASES["decider"]="executor"` preserved so historical receipt
  forensics still resolve.

### Files touched
- `backend/shared/roster.py` — ROLES, DEFAULT_ASSIGNMENTS, DEFAULT_ELIGIBILITY,
  legacy rewrite, eligibility hard-lock, swap/assign/eligibility canonicalization
- `backend/shared/seat_policy.py` — `strategist` policy row added; `auditor`
  row reinstated as real seat (no longer aliased to opponent)
- `backend/shared/mc_shelly.py` — POSITION_CODES adds `STR` (legacy `DEC` alias)
- `backend/shared/equity/council_policy.py` + `crypto/council_policy.py` —
  STACK_WEIGHTS `strategist: 0.90` (legacy `decider` retained)
- `frontend/src/components/RosterPanel.jsx` — STRATEGIST label, role lists
- `frontend/src/pages/BrainOperatorPage.jsx` — per-brain `expected_seats`
- Tests: `test_roster.py`, `test_seat_aliases.py`, `test_paradox_namespace.py`,
  `test_seat_policy_and_auto.py` updated to the new doctrine

### Verification
- 320/321 tripwires pass (1 pre-existing flaky seed-fixture test unrelated)
- Live API confirmed: `decider` ingress → `strategist` canonical; camaro→governor
  blocked (400); chevelle→strategist blocked (400)
- Lint clean (ruff)

---


## 2026-02-19 — Sidecar identity check-in surface (Portable Survival Layer companion)

P1 task closed: MC can now answer "who's PROD vs preview?" with one
query instead of grepping pod logs. Each brain sidecar POSTs its
boot-time `RuntimeStamp`; MC persists the latest stamp + verdict
(prod / preview / policy_drift / invalid / never) and renders the
roster on Diagnostics.

### Backend
* `shared/runtime/sidecar_checkin.py` — new module wiring three
  endpoints under `/api/admin/runtime/sidecar-checkin`:
    - `POST /sidecar-checkin/{brain}` (token-authed via
      `<BRAIN>_INGEST_TOKEN`) — sidecars call on boot/periodically.
      Validates against `RuntimeStamp.validate_for_prod_sidecar`,
      flags `policy_hash` drift vs MC's current `policy_hash()`, and
      upserts into the new `sidecar_checkins` collection.
    - `GET /sidecar-checkin` (admin JWT) — one row per known brain,
      verdicts: `prod` (clean), `preview` (env_name/mc_url drift),
      `policy_drift` (stamp valid but stale policy_hash), `invalid`
      (other validation failure), `never` (no check-in yet).
    - `GET /sidecar-checkin/{brain}` (admin JWT) — single-brain detail.
* `namespaces.py` — new collection constant `SIDECAR_CHECKINS`.
* `db.py` — unique index on `runtime` so upserts stay one-row-per-brain.

### Frontend
* `components/SidecarCheckinPanel.jsx` — auto-refreshes every 15s.
  Per-brain row: verdict chip, freshness band, hash-mismatch tag, all
  stamp fields (env_name, mc_url, db_name, broker_mode, git_sha,
  version, platform, exec_authority), plus a header summary
  (`N prod · N preview · N drift · N never`). Wired into Diagnostics
  above the existing patch-kit panel.

### Tests
* `tests/test_sidecar_checkin.py` — 11 tests covering token rejection,
  unknown-brain 404s, all four verdict paths, GET auth gate, brain
  coverage, freshness, and POST→GET roundtrip. All passing.
* Tripwire suite (`pytest -m tripwire`) — 116 passing, no regression.

### Doctrine pin
This panel is OBSERVABILITY ONLY. It surfaces drift to the operator
but does NOT gate execution — the broker still independently verifies
MC receipts (`shared/broker_router.py`) before any Alpaca/Kraken call.
Defense in depth: receipt seal blocks bad orders, check-in surface
makes the operator question "is alpha actually in PROD right now?"
a one-click answer instead of a Mongo grep.

### Alpha-side coupling
Once Alpha redeploys with the role adapter + RuntimeStamp from the
runtime patch kit, its boot-time POST will land here and the panel
will flip alpha from `never` → `prod` (or `preview` if the stack got
the env wrong). This replaces the manual Mongo grep step in Alpha's
verification checklist.

---


## 2026-02-17 (latest) — Three new risk guards + Position Monitor scheduler + P1 UI surfaces

Closed all P0 + P1 items from the fork plan in one pass.

### P0 — Risk Guards (Doctrine: Executors enter, lifecycle guards exit)

Added three deterministic guards joining the existing TakeProfit:

* `shared/risk/stop_loss_guard.py` — pure math, lane-neutral, returns
  CLOSE when pnl_pct ≤ -|stop_loss_pct|.
* `shared/risk/trailing_stop_guard.py` — pure math, stateful via
  `previous_peak`; inactive until `activate_after_pct` is reached;
  closes on drawdown from peak (LONG) or run-up from trough (SHORT).
* `shared/risk/max_hold_time_guard.py` — time-based discipline guard;
  closes when `(now - opened_at) ≥ max_hold_minutes`. Time-injectable
  (`now=` param) for deterministic tests.

Each guard has lane-isolated wrappers in `shared/equity/{guard}.py` and
`shared/crypto/{guard}.py` that look up the live position, call the
pure math, and (for `enforce_*`) actually close / reduce via
`shared.live_positions.close()` → broadcasts to `SHARED_OUTCOMES`.

Trailing-stop persists the running peak on the position doc
(`peak_price`, `peak_updated_at`) so the next tick sees today's
high-water without recomputing.

### P0 — Position Monitor scheduler (`shared/risk/position_monitor.py`)

Async background loop registered in `server.py` lifespan. Every
`POSITION_MONITOR_INTERVAL_SECONDS` (default 30s) it:

1. Snapshots open / managing positions from `shared_live_positions`.
2. Builds a per-tick equity price map via Alpaca's `list_positions()`.
   Crypto price oracle is stubbed pending Kraken `/Ticker`.
3. For each position, walks the four guards in **strict priority**:

       StopLoss → TakeProfit → TrailingStop → MaxHoldTime

   The **first non-HOLD verdict closes/reduces** and breaks out — lower
   priorities are not consulted on that tick (a stop-loss never races
   a take-profit on a whipsaw bar).
4. Writes an append-only audit row to
   `risk_monitor_evaluations` so the operator can see every decision.

Failure-isolated per position; one bad row never blocks the rest of
the loop. Env-tuneable (STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAIL_PCT,
TRAIL_ACTIVATE_PCT, MAX_HOLD_MINUTES). Disable with
`POSITION_MONITOR_ENABLED=false`.

### REST surface (`/api/admin/risk/...`)

Pure math (lane-agnostic):
* `POST /admin/risk/take-profit/evaluate`
* `POST /admin/risk/stop-loss/evaluate`
* `POST /admin/risk/trailing-stop/evaluate`
* `POST /admin/risk/max-hold-time/evaluate`

Lane-scoped check + enforce per guard:
* `POST /admin/risk/{equity|crypto}/{guard}/check/{position_id}`
* `POST /admin/risk/{equity|crypto}/{guard}/enforce/{position_id}`

Monitor control:
* `GET /admin/risk/monitor/status` — running flag, tick counters,
  config, priority array, doctrine string.
* `POST /admin/risk/monitor/run-once` — manual one-shot tick. Response
  shape: `{"summary": {open_positions, evaluated, actions_taken,
  errors}, "results": [...]}`.
* `GET /admin/risk/monitor/recent-evaluations` — append-only audit log
  for the UI.

### P1 — Risk Guard Status column on LivePositionsPanel

`LivePositionsPanel.jsx` now fetches `/admin/risk/monitor/recent-evaluations`
alongside the position list and renders a `GuardCell` per row:

* If a guard fired → colored badge (`stop_loss=red`, `take_profit=green`,
  `trailing_stop=amber`, `max_hold_time=purple`) + the reason tooltip.
* If every guard held → four colored pips (one per guard) + "ALL HOLD".
* If skipped (unknown lane, monitor hasn't ticked yet) → neutral "—".

Updates every 15s in sync with the position list.

### P1 — Brain × Lane policy toggle inside RosterPanel

New `BrainLanePolicyPanel` component appended to `RosterPanel.jsx`.
Renders a 4×2 matrix (alpha/camaro/chevelle/redeye × equity/crypto).
Each cell is a button that:

* Shows current state as `ALLOWED` (green) or `MUTED` (red).
* On click, POSTs to `/api/admin/brain-lane-policy` and refreshes.
* Cells with an explicit DB row are tagged `· explicit` (Camaro/crypto
  ships muted by seed).

Operator can now mute/unmute a brain per lane in one click — no curl.

### Tests added

* `/app/backend/tests/test_risk_guards.py` — 15 unit tests covering
  every (side × hit/miss × edge-case) combination for the three new
  guards. All deterministic, no DB.
* `/app/backend/tests/test_risk_monitor_and_policy.py` — 13 integration
  tests (Position Monitor REST + per-lane intents + brain-lane-policy
  CRUD lifecycle).
* All 22 unit tests + 13 integration = **35/35 passing**. Lane
  isolation guards still green.

### Doctrine pins

* No union endpoint that picks lane silently — every guard/enforce
  endpoint has the lane in the path.
* Priority order is fixed in code and exposed at
  `/admin/risk/monitor/status.priority` so the operator can verify.
* Crypto positions safely skip price-based guards when the price
  oracle is unavailable; MaxHoldTime still fires (time-only). This is
  the **MVP boundary** until Kraken `/Ticker` is wired.

---

## 2026-02-16 — Per-lane intent endpoints + visible crypto rejections

Two doctrinal gaps closed in one pass.

### Gap 1 — Crypto seat had no dedicated intent endpoint

Operator: *"crypto has its own seat now and that should have its own intent
just like its counterpart."*

Added per-lane endpoints, mirroring the per-lane risk-guards pattern:

```
POST /api/intents/crypto              (engine, X-Runtime-Token)
POST /api/intents/equity              (engine, X-Runtime-Token)
POST /api/admin/intents/crypto        (operator JWT)
POST /api/admin/intents/equity        (operator JWT)
```

Each is a thin lane-pinned wrapper around `post_intent` /
`admin_post_intent` (DRY: same gate chain, same broker_router, same
brain_lane_policy check). The path's lane is force-set on the body
before delegation; mismatched lanes 400 with a precise pointer to the
correct endpoint:

```
POST /api/admin/intents/crypto  body={lane:"equity", symbol:"AAPL"}
→ 400 "This endpoint accepts 'crypto' intents only; got lane='equity'.
        Use /api/intents/equity instead."
```

Generic `/api/intents` and `/api/admin/intents` preserved for
back-compat — existing brain sidecars keep working. New emitters should
target the per-lane endpoint matching their seat.

### Gap 2 — Camaro→crypto 403s were invisible

`brain_lane_policy` rejected Camaro crypto intents at ingest with HTTP
403 — *before* any DB write. Correct doctrine, but the operator had
zero record that Camaro tried. To the Intents UI, it looked like Camaro
never even attempted crypto.

Fix: every policy rejection now writes:

1. An **audit row** into `shared_intents` with:
   - `gate_state="rejected_at_ingest"`
   - `rejected_policy="brain_lane_policy"`
   - `may_execute=False`, `executed=False`, `audit_only=True`
2. An **mc_shelly** event with `event_type="intent_rejected_at_ingest"`
   so it shows up in the training-data substrate alongside successful
   emissions.

The 403 still fires — the rejection is unchanged. But it leaves a trace
now.

### Gap 3 — Intents UI had no lane filter

Added a **Lane** filter pill (all / equity / crypto) to the Intents page
and a **Lane** column to the table (blue=equity, purple=crypto badge).
`GET /api/intents` now accepts a `lane=` query param. Default is "all"
so the page works unchanged for existing operators; flipping to
"crypto" surfaces all crypto activity (including the new rejection
rows).

Added `"rejected_at_ingest"` to the gate-state filter pill so the
operator can isolate just-the-rejections in a single click.

### Verified

End-to-end smoke (preview):
- `POST /admin/intents/crypto REDEYE BTC/USD` → 200, intent persisted with lane=crypto, gate=pending
- `POST /admin/intents/crypto AAPL lane=equity` → 400, precise error pointing at /equity
- `POST /admin/intents/equity AAPL` → 200, intent persisted with lane=equity
- `POST /admin/intents/crypto Camaro ETH/USD` → 403, AND a `gate_state=rejected_at_ingest` audit row appears in `shared_intents`
- `GET /intents?lane=crypto` returns the full mix: REDEYE pending + Camaro rejections + historic equity-side
- `pytest tests/test_lane_isolation.py tests/test_take_profit_guard.py` → **7 passed in 0.02s**


## 2026-02-16 (latest) — Deterministic TakeProfitGuard installed (per-lane)

Operator: *"Add a deterministic TakeProfitGuard. … Give it to the executor
lane, yes — but not as 'executor opinion.' Use it as a mandatory post-entry
lifecycle guard."*

Doctrine pinned: **Executors enter. Lifecycle guards exit. Brains advise.
RoadGuard enforces.** Brains cannot override take-profit exits.

### Files added (4)

```
shared/risk/__init__.py
shared/risk/take_profit_guard.py     # pure deterministic math (snippet, verbatim)
shared/risk/routes.py                 # per-lane REST surface
shared/equity/take_profit.py          # Camaro's executor lane wrapper
shared/crypto/take_profit.py          # REDEYE's executor lane wrapper
tests/test_take_profit_guard.py       # 4 unit tests (snippet, verbatim)
```

### Why three layers (not one)

- **Lane-neutral math** in `shared/risk/take_profit_guard.py` — pure
  functions, no DB, no async, no LLM. Lives outside `shared/equity/` and
  `shared/crypto/` so the lane-isolation regression test allows both
  lanes to import from it without coupling to each other.
- **Per-lane wrappers** in `shared/equity/take_profit.py` and
  `shared/crypto/take_profit.py` — each adds the lane's position
  bookkeeping (filter `lane='equity'` vs `lane='crypto'`, read entry
  price from open fill, call `live_positions.close` /
  `record_management` with the verdict's fraction).
- **Per-lane REST endpoints** under `/api/admin/risk/equity/...` and
  `/api/admin/risk/crypto/...` — NO union endpoint that silently picks
  the lane. The caller must address the right lane.

### REST surface

```
POST  /api/admin/risk/take-profit/evaluate                        (pure math, lane-agnostic)
POST  /api/admin/risk/equity/take-profit/check/{position_id}      (read-only preview, equity)
POST  /api/admin/risk/equity/take-profit/enforce/{position_id}    (acts: REDUCE/CLOSE)
POST  /api/admin/risk/crypto/take-profit/check/{position_id}      (read-only preview, crypto)
POST  /api/admin/risk/crypto/take-profit/enforce/{position_id}    (acts: REDUCE/CLOSE)
```

`enforce` calls `live_positions.close` (terminal) or
`live_positions.record_management` (REDUCE), depending on the deterministic
verdict. Both broadcast to `shared_brain_outcomes` so the scorecard pipeline
captures the exit. Brain advisory cannot override this path — caller is
authoritative, guard is deterministic.

### What's still pending

This install gives you the **callable guard**. The natural next layer is the
**Position Monitor loop** the operator's diagram references — a background
task that polls open positions every N seconds, fetches current price, and
calls `enforce_position` per lane. Today the guard is invoked by:
- The operator (manually, via curl/Postman)
- The executor sidecars (when REDEYE/Camaro sees a new bar and wants to
  check its open positions)

Building the monitor loop is a separate piece. Recommend wiring it next so
the guard runs without human/sidecar intervention.

### Verified

- `pytest tests/test_take_profit_guard.py` → **4/4 PASS** (LONG hit, SHORT
  hit, partial REDUCE, no-trigger HOLD)
- `pytest tests/test_lane_isolation.py` → **3/3 PASS** (new files respect
  the lane-isolation doctrine — neither lane imports the other)
- `POST /api/admin/risk/take-profit/evaluate` LONG 100→103 @ 3% target
  → returns `{action: "CLOSE", reason: "Take-profit target hit at 3.00%",
  pnl_pct: 3.0, target_pct: 3.0, close_fraction: 1.0}` ✓
- Backend boots clean


## 2026-02-16 (late) — Lane-isolation regression test installed

Operator: *"That caveat is exactly how this bug came back before: crypto path
accidentally calls equity executor helper. Add the guard so future code
cannot quietly re-couple the lanes."*

**New file:** `backend/tests/test_lane_isolation.py` (3 guards)

```
test_crypto_lane_does_not_import_equity_authority
test_equity_lane_does_not_import_crypto_authority
test_crypto_modules_do_not_call_legacy_get_executor_holder
```

Walks `shared/crypto/` and `shared/equity/` recursively. Any module under
those roots that:
- imports from the OTHER lane's subpackage, OR
- imports `get_executor_holder` (equity-only helper) into the crypto tree, OR
- references `kraken` from the equity tree, OR
- calls `get_executor_holder(` literally in the crypto tree

… fails the test with a precise offender path + pattern.

**Verified:**
- All 3 guards PASS today (0.01s).
- Negative test: injected `from shared.executor_seat import get_executor_holder`
  into `shared/crypto/exposure_caps.py` → guard FAILED with
  `AssertionError: /app/backend/shared/crypto/exposure_caps.py: forbidden
  'from shared.executor_seat import get_executor_holder'`. Reverted; green again.

**Wire into CI**: Run `pytest tests/test_lane_isolation.py -q` from
`/app/backend` as part of any pre-deploy gate. With pytest already in
dependencies, this is zero-config.

Doctrine locked:
- equity seat cannot execute crypto
- crypto seat cannot depend on equity
- lane authority stays lane-owned


## 2026-02-16 (very late) — Lane bleed scrubbed from ingest + gate chain messaging

Operator's question: "Why is [the crypto intent path] going past the equity
executor seat? If they're separate why would the executor seat for crypto
need permission from the equity seat?"

Correct read — there was residual equity-side leakage in two places, surviving
this morning's earlier seat-snapshot fix:

### Issue 1 — Ingest stamped equity executor as `executor_holder_at_post`

Both intent-post paths (`POST /api/intents` and `POST /api/admin/intents`)
called `get_executor_holder()` unconditionally to populate
`executor_holder_at_post`. That helper only reads the equity executor seat
doc, so a REDEYE crypto intent ended up stamped:

```
executor_holder_at_post: "alpha"   # equity holder — meaningless for crypto
```

Audit fields lied about authority on every crypto intent.

### Issue 2 — Gate chain fallback message also referenced the equity seat

`execution.py:_evaluate_gates` had a legacy fallback:
```python
if current_holder is None:
    current_holder = await get_executor_holder()
```
And the final error branch read:
```
f"Execute-seat was held by {held_at_post} at post time, not {intent_stack}"
```
For a crypto intent with no crypto seat held, this message would surface the
**equity** holder — telling the operator REDEYE crypto was blocked by an
Alpha-shaped problem. Not true; the lanes are independent.

### Fix

`shared/intents.py` (both paths):
- Compute `executor_at_post` by walking `seats_with_execute(intent_lane)` and
  recording the holder of the lane-appropriate execute seat. For crypto,
  that's the `crypto` seat holder. For equity, that's the `executor` seat
  holder. The legacy `get_executor_holder()` is no longer called at ingest.
- Drop the loop's `break` so we record the lane-appropriate holder even
  when it's not the emitting brain — still gives the gate chain a sensible
  value for the fallback message.

`shared/execution.py:_evaluate_gates`:
- Removed the equity-lookup fallback.
- Rewrote the vacant-seat message to be lane-aware:
  `"No execute-seat was held for lane='crypto' when intent was posted — seat vacant, no authority"`.
- Rewrote the wrong-brain message to be lane-aware:
  `"Execute-seat for lane='crypto' was held by <X> at post time, not <Y>"`.

### Verified (preview)

Fresh REDEYE BUY BTC/USD crypto intent — persisted doc inspection:
```
stack:                     redeye
lane:                      crypto
seat_at_post_time:         opponent       (REDEYE's permanent equity-roster role)
executor_holder_at_post:   redeye         ← was 'alpha' before fix; now lane-aware
holds_executor_seat:       true
matched_seat_at_post:      crypto
```

Dry-run gate chain:
```
PASS  executor_seat_check  redeye holds the 'crypto' seat (lane=crypto); held at ingest
```

Zero equity-side references in any crypto intent's audit trail or gate
output from this point forward.


## 2026-02-16 (very late) — `redeye_crypto_intent_bridge` installed

Operator pasted a snippet and said "install it." The snippet was diagnosing
a bug in REDEYE-side code (hardcoded `requires_final_authority: "camaro"`),
which does NOT exist in MC. But the snippet's intent — *seat-based final
authority, no Camaro hardcoding* — was correct and worth installing as an
MC-side bridge.

**New module:** `backend/shared/redeye_crypto_intent_bridge.py`

Adapts the snippet's design to MC's real schema and API:
- Snippet called `get_executor_holder(lane="crypto")` (signature doesn't
  exist in MC). Bridge uses MC's real helpers: `seats_with_execute("crypto")`
  + `get_seat_holder(seat)`.
- Snippet's intent shape used REDEYE-only fields (`requires_final_authority`,
  `requires_roadguard`, etc.). Bridge stamps BOTH the snippet's
  doctrine fields AND MC's canonical fields (`stack`, `rationale`,
  `lane`, etc.) so the intent reads correctly to both auditors.

**Doctrine guards (preserved verbatim from snippet):**
- `crypto_only` — non-crypto symbols rejected (400)
- `intent_only` — `may_execute=False`, `requires_gate_pass=True` pinned
- `hold_not_promotable` — HOLD action rejected (action Literal excludes it)
- `seat_based_final_authority` — recipient resolved dynamically from roster
- `crypto_roadguard_required` — stamped on every emitted intent

**REST surface mounted under `/api/admin/redeye/bridge`:**
- `GET  /authority` — returns the brain holding the crypto execute seat
- `POST /emit` — REDEYE decision → MC intent

**Verified live (preview):**
- `GET /authority` → `{lane:"crypto", final_authority:"redeye", seat_vacant:false, authority_model:"seat_based"}`
- `POST /emit BTC/USD SHORT conf=0.78` → intent persisted, `requires_final_authority="redeye"` (matched the crypto seat holder)
- `POST /emit TSLA BUY` → HTTP 400 "does not look like crypto"
- `POST /emit BTC/USD HOLD` → HTTP 422 (Literal rejects)

**Authority is resolved at emit time** — rotate the crypto seat, the next
emitted intent stamps the new holder. No code changes needed for rotation.

**What this does NOT do (operator awareness):**
- It does NOT auto-promote REDEYE opinions into intents. That would be a
  scheduler, not yet built. Today the bridge is callable surface only — a
  caller (REDEYE's sidecar OR an operator OR a future scheduler) has to
  POST a decision to it.
- It does NOT bypass the gate chain. Intents emitted through the bridge
  still go through `executor_seat_check`, `broker_connected`, lane caps,
  governance multipliers, etc. — same path as any other intent.


## 2026-02-16 (very late) — REDEYE crypto unblock: lane-aware seat snapshot at ingest

Operator reported REDEYE crypto intents still being blocked despite holding
the `crypto` seat in prod. Root-caused, fixed, verified.

### The bug

In `shared/intents.py`, the ingest-time seat snapshot called
`get_executor_holder()`, which **only** reads the legacy single-seat equity
executor doc. A REDEYE crypto intent — where REDEYE legitimately holds the
`crypto` seat (which has `may_execute=True, lane_scope=["crypto"]`) — got
stamped:

```
holds_executor_seat: false
executor_holder_at_post: <whoever held equity executor>
```

The gate chain's `executor_seat_check` correctly walks `seats_with_execute(lane)`
and finds REDEYE on `crypto`, so `holds_now=True`. But because
`held_at_intent=False` was frozen into the intent at ingest, the conditional
cascade fell through to the last branch:

> *"Execute-seat was held by [equity_holder] at post time, not redeye"*

Audit-correct (you can't rescue an intent posted without authority), but the
authority check itself was lane-blind. So **every** lane-isolated brain's
intents failed gate 3 by construction.

### The fix

`shared/intents.py` — both engine path (POST `/api/intents`) and admin proxy
path (POST `/api/admin/intents`):

```python
from shared.executor_seat import seats_with_execute, get_seat_holder
holds_executor = False
matched_seat_at_post = None
for _seat_name in seats_with_execute(effective_lane):
    _h = await get_seat_holder(_seat_name)
    if _h == body.stack:
        holds_executor = True
        matched_seat_at_post = _seat_name
        break
```

Now: REDEYE→crypto checks both `executor` (no, that's Alpha's equity seat) AND
`crypto` (yes, REDEYE holds it) → `holds_executor_seat=True`,
`matched_seat_at_post="crypto"`.

Also added `matched_seat_at_post` to the persisted intent doc so future audits
show **which** execute-capable seat was held, not just a boolean.

### Verified (preview)

Fresh REDEYE BUY BTC/USD crypto intent → dry-run:
```
PASS   executor_seat_check    redeye holds the 'crypto' seat (lane=crypto); held at ingest
```

The previously-stuck "Execute-seat was held by camaro at post time, not redeye"
is gone. Only remaining block is `broker_connected` — which is a preview-env
artifact (no Kraken keys in preview DB). In prod (Kraken LIVE, REDEYE on crypto
seat), the same intent would pass every gate.

### What this means for prod

Once you redeploy this fix:
- REDEYE crypto intents posted via `POST /api/intents` will pass gate 3.
- Auto-router (running every 30s) will pick them up and route to Kraken.
- $30 → $22.50 effective notional (governance downsizing from Chevelle's
  no-stance soft downweight × quantum entropy of 0.95).

**Backfill question for the operator**: existing pending crypto intents from
REDEYE in prod were stamped `holds_executor_seat=False` under the old code.
They will continue to fail gate 3 even after the fix. Options:
1. Let them die (clean slate; brain will emit new ones).
2. Re-stamp them with a one-shot script that recomputes the seat snapshot
   under the new logic. Trivial to write.

Recommend (1) — old intents are stale market context anyway.


## 2026-02-16 (later) — Lane code separation: `shared/crypto/` + `shared/equity/`

Operator pushed back on equity-and-crypto living in the same folder.
Reshuffled per option (a) — files moved, imports rewired, zero behavior
change.

**New subpackages:**

```
shared/crypto/
├── __init__.py
├── kraken.py            (was shared/kraken.py)
├── routes.py            (was shared/kraken_routes.py)
├── broker_adapter.py    (was shared/broker/kraken_adapter.py)
├── council_policy.py    (extracted from shared/council.py)
└── exposure_caps.py     (crypto $30/order cap extracted from shared/exposure_caps.py)

shared/equity/
├── __init__.py
└── council_policy.py    (extracted from shared/council.py)
```

**Dispatcher invariant** — a lane-only change requires editing ONLY
that lane's subpackage:
- Crypto-only tuning: edit `shared/crypto/*` — never touches equity.
- Equity-only tuning: edit `shared/equity/*` — never touches crypto.
- `shared/council.py` is now a 12-line dispatcher importing both
  policies; nothing else changes there.
- `shared/exposure_caps.py` imports `CRYPTO_PER_ORDER_USD` from
  `shared/crypto/exposure_caps.py` — same dispatch pattern.

**Imports rewired (4 sites):**
- `server.py` — kraken router import
- `shared/broker_router.py` — kraken adapter import
- `shared/exposure_caps.py` — crypto cap import (now from crypto subpkg)
- `tests/test_kraken.py` — `_sign` import
- `shared/council.py` — `EQUITY_POLICY` + `CRYPTO_POLICY` imports

**Verified (preview):**
- Backend boots clean. Logs confirm Kraken router + brain-lane policy
  seed both still ran.
- All 6 sanity endpoints respond 200 (health, kraken/status,
  exposure-caps, brain-lane-policy, roster, council/lookup-debug for
  BTC/USD on crypto lane).
- REDEYE crypto dry-run re-run post-reorg: identical gate-chain
  verdict, identical risk-multiplier (0.75), identical caps
  (`caps.crypto: 30.0` now sourced from the new file).

Net: same behavior, cleaner physical layout. A crypto governance tune
no longer requires the operator (or the next agent) to scroll past
equity logic to find the knob.


## 2026-02-16 (late) — Per-brain × lane intent-emission policy + Camaro→crypto muted

Operator asked to "turn off Camaro's crypto trading". Built a per-brain × lane
ingest policy that blocks intents at the boundary (rather than letting them pile
up at `gate_state=pending`).

**New module:** `shared/brain_lane_policy.py`
- Collection: `brain_lane_policy` — one row per (brain, lane) override
- Helper: `is_brain_lane_allowed(brain, lane) -> bool` (default allow)
- REST: `GET/POST /api/admin/brain-lane-policy`, `DELETE /api/admin/brain-lane-policy/{brain}/{lane}`
- Seed: Camaro→crypto = `allowed: false` (idempotent, runs in lifespan)

**Wired into both intent POST paths:**
- `POST /api/intents` — engine-side brain ingest. 403 before any DB write.
- `POST /api/admin/intents` — operator-proxy ingest. Same guard.

**Why a separate policy (not eligibility):**
Eligibility governs WHICH SEATS a brain may hold. Lane policy governs whether
a brain may even POST an intent for a given lane. Both have legitimate uses:
- A brain might be `crypto_opponent`-eligible (voicing setups for the seat
  holder to evaluate) but blocked from POSTing crypto intents directly.
- That's the Camaro situation today.

**Verified (preview):**
- Backend reboot: "Brain × lane emission policy seeded"
- `GET /api/admin/brain-lane-policy` returns the seed + effective matrix
- Camaro→crypto POST → HTTP 403 with clean error message
- Camaro→equity POST → HTTP 200, intent created normally
- Policy persists across backend restarts (DB-backed, not env)

**Operator levers:**
- Re-enable Camaro→crypto: `DELETE /api/admin/brain-lane-policy/camaro/crypto`
  (or POST with `allowed: true`)
- Block any other (brain, lane) pair the same way
- View the effective matrix at any time via `GET /api/admin/brain-lane-policy`

**178 historical pending crypto intents from Camaro in preview DB** are left
intact — they're audit history (every one of them was correctly blocked at
`executor_seat_check`). The VRL gate scorecard will pick them up.


## 2026-02-16 — Two long-standing engine-side issues RESOLVED (operator confirmed)

The operator confirmed end-of-day that the external brain engines are now healthy.
Marking both items closed so the next agent doesn't chase ghosts:

- ✅ **Camaro double-pinging / pointed at Preview URL** — engine sidecar's
  `MC_BASE_URL` is now correctly set to production. The "Preview Drift" banner
  on `/admin/diagnostics` was the right surface; the actual fix was external.
- ✅ **`httpx` keep-alive sidecar freeze** — the hardening patch was applied
  external to MC. Brain disconnects no longer recurring.


## 2026-02-16 (post-batch) — Pro Max chat endpoint retired

Per operator direction: the main risedual.ai site hosts its own chat
surface; MC is admin-only and does not need to be a chat backend. The
P3 refactor of `chat.py` from earlier today became moot.

**Removed:**
- `backend/shared/public_api/chat.py` — deleted entirely.
- `backend/shared/public_api/router.py` — dropped the `chat_router`
  import + include.
- `backend/namespaces.py` — dropped the `PUBLIC_CHAT_MESSAGES`
  constant (replaced with a doc-only note explaining the retirement).
- `backend/requirements.txt` — dropped the `anthropic==0.102.0` line I
  added earlier today. SDK uninstalled from the venv (`pip uninstall
  anthropic docstring-parser`).

**Left intact:**
- The MongoDB collection `public_chat_messages` was NOT dropped — that's
  operator territory. The collection is no longer written to. Drop with
  `db.public_chat_messages.drop()` from mongosh when convenient.
- `emergentintegrations` is still in `requirements.txt` because
  `narrative.py` still depends on it for the digest summary cache.

**Verified:**
- Backend restarts clean. `/api/health` returns 200.
- `POST /api/public/chat` now returns 404 (route gone, as expected).


## 2026-02-16 — P1 + P3 batch: Live Positions UI, VRL Scorecards UI, nightly scheduler, vendor SDK chat

Four follow-on tasks from the Saturday Sprint. All verified.

### P1 — LivePositionsPanel UI

New component `frontend/src/components/LivePositionsPanel.jsx` (~360
lines) wired into `/admin/overview` (above FeedersStrip). Lists every
live position with state-filter chips (open / managing / closed / all),
auto-refresh every 15s, totals header, and the doctrine reminder
"close broadcasts to shared_brain_outcomes". Two modals:

- **Manage modal** — note (required) + delta notional (optional). Hits
  `POST /api/admin/live-positions/{id}/manage`.
- **Close modal** — pnl_usd / pnl_pct / outcome_label / note. The label
  field auto-derives a preview from pnl (win/loss/scratch). Hits
  `POST /api/admin/live-positions/{id}/close`.

Verified: panel renders on `/admin/overview` with the empty-state
"— no positions in this state —" and all `data-testid`s resolve.

### P1 — VRLScorecardsPanel UI

New component `frontend/src/components/VRLScorecardsPanel.jsx` (~240
lines) wired into `/admin/diagnostics` (after the QuantumPanel).
Sortable table with gate / sample / precision / recall / accuracy /
TP·FP·TN·FN / verdict columns. Tier coloring uses three thresholds:

- ≥70% precision → EFFECTIVE (green)
- ≥50% precision → MIXED (amber)
- <50% precision → FRICTION (red)

Default sort is precision ascending (worst first) so the operator sees
underperforming gates at the top. Shows a live scheduler status badge
("RUNNING every 24h · rolling 720h") fed from
`GET /api/admin/vrl/scheduler/status`. Recompute button triggers
`POST /api/admin/vrl/scorecards/recompute` with the operator-set window.

### P3 — Nightly scorecard scheduler

`shared/vrl.py` gained `start_scorecard_scheduler` /
`stop_scorecard_scheduler` (mirrors the auto_router pattern). Wired into
`server.py` lifespan. Env knobs:

- `VRL_SCHEDULER_ENABLED` (default `true`)
- `VRL_SCHEDULER_INTERVAL_HOURS` (default `24`)
- `VRL_SCHEDULER_WINDOW_HOURS` (default `720` / 30 days)

First run delayed 5 minutes after boot so the rest of the system warms
up first. New endpoint `GET /api/admin/vrl/scheduler/status` for the UI.
Verified live: scheduler logs "vrl scheduler started: interval=24h
window=720h" on boot; status endpoint returns `running=true`.

### P3 — chat.py refactored to Anthropic vendor SDK

`shared/public_api/chat.py` (~340 lines) rewritten away from
`emergentintegrations.llm.chat.LlmChat` to the official
`anthropic.AsyncAnthropic` SDK per the latest playbook from
integration_playbook_expert_v2.

Key changes:
- `pip install anthropic==0.102.0`; added to `requirements.txt`.
- Singleton `AsyncAnthropic` client, lazily instantiated on first request
  so the import doesn't fail when the key is missing (endpoint returns
  503 instead, matching legacy semantics).
- History replay now uses a **native** alternating user/assistant
  `messages=[…]` list — the legacy implementation stuffed all prior
  turns into a synthetic preamble on the LATEST user message, which was
  worse for token cost AND made `stop_reason` / `usage` invisible. The
  new path returns `stop_reason`, `input_tokens`, `output_tokens` on
  the `ChatResponse`.
- System context (live MC positions + indicator snapshots) goes into
  the `system=` field — not into the user message — so the model
  treats it as the operator frame.
- Direction-aware error handling: `RateLimitError` → 429,
  `APIConnectionError` → 503, `APIStatusError` → 502.
- Model is now env-overridable: `CLAUDE_MODEL_ID` (default
  `claude-sonnet-4-5-20250929`). Output cap env-overridable too:
  `CLAUDE_MAX_OUTPUT_TOKENS` (default 1024).

**REQUIRES**: user must add `ANTHROPIC_API_KEY=sk-ant-...` to
`backend/.env` before the chat endpoint will serve real LLM responses.
Without it, the endpoint returns 503 with the message "LLM not
configured (ANTHROPIC_API_KEY unset in backend/.env)" — same operational
posture as the prior `EMERGENT_LLM_KEY unset` 503.

The legacy `EMERGENT_LLM_KEY` is no longer read by chat.py and can be
removed once the operator confirms the new vendor key is in place.

**Files added:**
- `frontend/src/components/LivePositionsPanel.jsx` (~360 lines)
- `frontend/src/components/VRLScorecardsPanel.jsx` (~240 lines)

**Files changed:**
- `backend/shared/vrl.py` — scheduler + status endpoint
- `backend/server.py` — start/stop scheduler in lifespan
- `backend/shared/public_api/chat.py` — full vendor-SDK refactor
- `backend/requirements.txt` — `anthropic==0.102.0`
- `frontend/src/pages/Overview.jsx` — mount LivePositionsPanel
- `frontend/src/pages/Diagnostics.jsx` — mount VRLScorecardsPanel


## 2026-02-16 — Saturday Sprint P0 + P2 batch shipped

Five tasks landed in one pass. All verified via direct API + Python smoke
tests; backend restarted clean.

### P0 — Live Position Lifecycle (open → managing → closed)

New module `shared/live_positions.py` + new collections
`shared_live_positions` and `shared_live_position_audit`. The doctrine
follows the user direction: this is a **separate** collection from the
discussion-thesis `shared_positions` (option B from clarification), with
every transition recorded under MC Shelly guidelines (event types
`position_opened`, `position_managing`, `position_closed`, each carrying
the full roster snapshot + regime_fp).

- `open_from_receipt(receipt, intent)` is idempotent on `receipt_id` —
  re-runs are safe. Hooked into both the operator-confirmed path
  (`shared/execution.py:execution_submit`) and the auto-router
  (`shared/auto_router.py:_route_one`).
- `record_management(...)` records scale-ins, scale-outs, stop moves.
  Transitions `open → managing` on first call, stays in `managing`
  thereafter.
- `close(...)` is terminal. Auto-labels (win/loss/scratch) from pnl_usd
  if the operator didn't supply one, then writes a `shared_brain_outcomes`
  row so the existing scorecard pipeline (hit-rate, brier, regime
  breakdown) picks up the trade automatically. Outcome broadcast is
  one-shot per position.
- REST surface: `/api/admin/live-positions` (list + per-id),
  `/api/admin/live-positions/{id}/manage`, `/api/admin/live-positions/{id}/close`.

End-to-end smoke test passed: open ($100 BUY AAPL) → manage (-$30 scale
out) → close (+$12.50 pnl) → outcome broadcast confirmed with label='win'.

### P0 — regime_fp 6-key fingerprint

`shared/hypothesis.py:_regime_fingerprint` upgraded from 3 → 6 keys. Adds
`trend_direction` (vs SMA50 / EMA20), `volume_band` (vs 20-day avg
volume), `volatility_band` (ATR% bucket). New constant
`hypothesis.REGIME_FP_KEYS` is the canonical key set.

`IntentIn.evidence` now validates that any submitted `regime_fp` only
uses canonical keys — unknown keys reject with HTTP 422. Missing keys
are tolerated and back-filled by `shared/intents.py:_enrich_regime_fp`
at ingest time using the latest indicator snapshot for the symbol.
Brain-supplied keys win over server-derived (no silent overwrites).

Wired into both `POST /api/intents` and `POST /api/admin/intents`.

### P2 — `/api/health` deploy_mode now derived

Cosmetic prod bug fixed: `/api/health` no longer hard-codes
`deploy_mode` from the env var. It now reports the union — if **either**
the `DEPLOY_MODE` env var or a broker's `execution_enabled=True` is
set, returns `"execution"`. Otherwise `"observation"`. The endpoint
also surfaces both inputs (`deploy_mode_env`, `deploy_mode_derived`) so
the operator can see which signal won.

### P2 — Verified Reinforcement Layer (VRL)

New module `shared/vrl.py` + collections `shared_vrl_verifications`,
`shared_vrl_scorecards`.

1. **Per-receipt verifications**: `verify_receipt(receipt, intent)` runs
   on every executed receipt (idempotent on `receipt_id`). Captures
   direction-aware slippage, notional drift, fill quality. Wired into
   both execution paths.
2. **Per-gate scorecards**: `recompute_scorecards(window_hours)` joins
   `shared_gate_results` × `shared_brain_outcomes` on `intent_id` and
   tallies a TP/FP/TN/FN confusion matrix per gate name. Surfaces
   precision (the "net protect rate"), recall, accuracy. Operator
   triggers via `POST /api/admin/vrl/scorecards/recompute`.

REST: `/api/admin/vrl/verifications`, `/api/admin/vrl/verify`,
`/api/admin/vrl/scorecards`, `/api/admin/vrl/scorecards/recompute`.

### P2 — Master Design System

`/app/design_guidelines.md` (260 lines). Single source of truth for the
RISEDUAL aesthetic: color tokens (`rd-*`), typography hierarchy, lane
colors, three-tier heartbeat doctrine, motion guidelines, `data-testid`
discipline, forbidden patterns. Now exists so the next agent doesn't
re-derive conventions from scratch.

**Files added:**
- `backend/shared/live_positions.py` (~430 lines)
- `backend/shared/vrl.py` (~310 lines)
- `design_guidelines.md` (~260 lines)

**Files changed:**
- `backend/namespaces.py` — 4 new collection constants
- `backend/server.py` — `/api/health` derivation, mount 2 new routers
- `backend/shared/hypothesis.py` — `_regime_fingerprint` 6-key, exported `REGIME_FP_KEYS`
- `backend/shared/intents.py` — validator + `_enrich_regime_fp`, wired in both intent posts
- `backend/shared/execution.py` — hooked `open_from_receipt` + `verify_receipt`
- `backend/shared/auto_router.py` — same hooks on auto-routed receipts

**API endpoints added:** 7 (`/api/admin/live-positions` × 4, `/api/admin/vrl/*` × 4 minus one alias)


## 2026-02-16 — RosterPanel dual-lane (EQUITY | CRYPTO)

Updated `frontend/src/components/RosterPanel.jsx` to render the cross-lane
multi-seating model added 2026-02-15. Two lanes are now visible side-by-side:

- EQUITY LANE (5 seats): decider, executor, governor, advisor, opponent
- CRYPTO LANE (4 seats): crypto (executor), crypto_governor, crypto_advisor, crypto_opponent

The picker now surfaces cross-lane state clearly: when a candidate brain already
holds a seat in the *same* lane, the chip warns "will vacate <role>" (backend
auto-vacates intra-lane). When they hold a seat in the *other* lane, the chip
shows "also holds <role> (<lane>)" — no vacation needed, cross-lane is allowed.
The eligibility matrix gained a two-row header grouping EQUITY vs CRYPTO so all
36 cells (4 brains × 9 roles) are scannable.

**Files changed:**
- `frontend/src/components/RosterPanel.jsx` — full rewrite (~395 lines)

**Verified:**
- GET /api/admin/roster returns all 9 roles
- All 9 roster-slot-* testids resolve on /admin/overview
- Cross-lane assignments persisted (chevelle: governor + crypto_governor)

## 2026-02-16 — execution.py post-extraction cleanup

Removed 6 residual unused imports from `shared/execution.py` left over after
the council/quantum extraction (council moved to `shared/council.py` on
2026-02-15). Hoisted the council re-export block to the top-of-file import
section to clear the E402 module-level-import-not-at-top warning. File is now
639 lines (down from 1355 pre-extraction) and `ruff check` returns clean.

**Files changed:**
- `backend/shared/execution.py` — import cleanup only, no behavior change


# CHANGELOG — RiseDual Mission Control

Append-only. Newest at top.

## 2026-02-14 — AI Investment Hypothesis Engine (Brain Recall, no external LLMs)

Standalone research tool at `/admin/hypothesis`. Operator types a ticker → MC aggregates that brain's own pushed content. **No external AIs involved** (operator constraint).

**Backend additions:**
- `/app/backend/shared/auditor_seat.py` — rotatable Auditor seat (mirrors Executor seat). `GET /api/auditor`, `POST /api/auditor/rotate`, `GET /api/auditor/audit`
- `/app/backend/shared/hypothesis.py` — `POST /api/hypothesis/analyze {symbol}` is now PURE RECALL over MongoDB. Aggregates per role (Strategist = Executor seat brain, Auditor = Auditor seat brain):
  - `latest_intent` from `shared_intents` (action/confidence/rationale/evidence/gate_state)
  - `latest_opinion` from `shared_brain_opinions` (topic = `symbol:<S>`)
  - `shelly_memories` from `shared_labeled_memories` — that brain's gated/labeled memory entries referencing the symbol
  - `track_record` from `shared_brain_outcomes` (wins/losses + last 5)
  - `similar_setups` — brain's past executed intents on OTHER symbols matching current regime fingerprint (RSI band, MACD hist sign, BB position)
  - Plain-string `summary` headline composed deterministically — no LLM
- New collection: `hypothesis_analyses` (audit log only — no LLM content)

**Performance:** 174ms typical (was 16s with Claude+Gemini). 5 brain-content sections per card.

**Frontend additions:**
- `/app/frontend/src/pages/Hypothesis.jsx`: ticker search + Analyze/Clear buttons, dual cards:
  - **Strategist (green, Sparkle icon)** — Latest Intent · Discussion Stance · Shelly Memories · Track Record · Similar Past Setups
  - **Auditor (red, ShieldWarning icon)** — same five sections, brain-content-only
  - Brain badge + 1-line plain summary per card
  - Each section uses brain's PROPER colour for the eyebrow + count
- Client-side 30-min `Map<symbol, {result, expiresAt}>` cache; "CACHED · expires in Xm" indicator
- `Hypothesis` nav item in admin sidebar with Sparkle icon

**Initial seat assignment:**
- Executor: CAMARO
- Auditor: REDEYE (newly assigned 2026-02-14)

**Doctrine preserved:**
- No outside AIs (no Claude / Gemini / GPT). Only brain content surfaced.
- Each brain "explains based on memories of similar situations" via `similar_setups` regime-fp recall.
- Seats are rotatable; rotating a brain into a seat instantly changes the Hypothesis voice.




## 2026-02-14 — Alpaca Paper Broker + Real Execution Pipeline (Week 1, Day 1)

MC now owns a broker. Intents that pass the full gate chain route to **Alpaca paper** as $10 notional market-day orders. No brain ever sees broker keys.

**New backend modules:**
- `/app/backend/shared/broker/__init__.py`, `base.py`, `alpaca.py`, `alpaca_routes.py` — `BrokerAdapter` ABC + `AlpacaPaperAdapter` (wraps `alpaca-py 0.43.4`, `paper=True` hard-coded) + admin connect/status/test/account/positions/orders/disconnect endpoints
- `/app/backend/shared/exposure_caps.py` — hardcoded rails: **$10/order, $50/day, $100 open notional**. No operator surface to relax them (change-and-redeploy)
- `/app/backend/shared/execution.py` — full 8-gate chain (schema_invariants · action_routable · executor_seat_check · live_trading_disabled · broker_connected · cap_per_order · cap_per_day · cap_open_notional) + `/api/execution/{dry_run, submit, receipts, caps}`. Submit requires `confirm="execute"` and stamps an execution receipt; intents are idempotent (409 on re-submit)

**New endpoints:**
- `POST /api/admin/alpaca/connect` — Fernet-encrypted key storage; probes ping BEFORE persisting
- `GET  /api/admin/alpaca/status` — redacted preview + last_ping
- `POST /api/admin/alpaca/test` — cheap broker ping
- `GET  /api/admin/alpaca/{account,positions,orders}` — broker reads
- `DELETE /api/admin/alpaca/{disconnect,orders/<id>,positions/<symbol>}`
- `POST /api/execution/dry_run?intent_id=&order_notional_usd=` — gate chain evaluation only
- `POST /api/execution/submit` — gated order routing, `confirm="execute"` required
- `GET  /api/execution/{receipts,caps}` — operator visibility

**Frontend:**
- `/app/frontend/src/components/AlpacaConnect.jsx` — credentials modal + status tile, mounted on `/admin/intents` below the Executor Seat tile. Shows acct, equity, daily-spend / cap, open-notional / cap, last-ping
- `/app/frontend/src/pages/Intents.jsx` — each intent row gains a **submit** button when gate_state is dry_run_passed/passed; executed intents show a green executed badge with the broker_order_id in the detail panel
- `/app/frontend/src/lib/api.js` — fetch wrapper now surfaces backend `detail` strings in `err.message` (no more "HTTP 400" placeholder)

**DB collections:**
- `alpaca_credentials` (singleton, Fernet-encrypted at rest)
- `alpaca_audit_log` (every state change)
- `execution_receipts` (one row per routed order)

**Tests:**
- `tests/test_alpaca_broker.py` — 6 unit tests (mocked SDK)
- `tests/test_execution_gates.py` — 8 gate-chain unit tests
- testing-agent integration suite: 10/10 new + 14/14 unit pass

**Doctrine preserved:**
- Brains do NOT execute. Only MC routes orders.
- Executor seat held + still held = required at submit time. Stale rotations block.
- LIVE_TRADING_ENABLED stays False. Live broker is a separate adapter.



## 2026-02-13 — Patch distribution channel + Decision Machine v1.0

MC now serves drop-in code patches over HTTPS. Brains pull their own updates via `X-Runtime-Token` auth — no copy-paste required. First patch published: **Decision Machine** (intent envelopes).

**New endpoints:**
- `GET  /api/patches` — list available patches
- `GET  /api/patches/{name}/manifest` — file list with sha256 + bytes
- `GET  /api/patches/{name}/file/{filepath:path}` — raw file content + sha256
- `GET  /api/patches/install.sh` — bash installer (curl-pipe-bash compatible)
- `POST /api/intents` — brain emits an intent envelope (schema-pinned safety)
- `GET  /api/intents` — read intents (any brain token or admin)
- `POST /api/admin/intents` — operator proxy emission
- `POST /api/execution/dry_run` — runs gate chain stub against an intent_id

**One-liner install** from any brain:
```bash
curl -s "$MC/api/patches/install.sh" -H "X-Runtime-Token: $TOKEN" \
  | bash -s -- decision_machine ./services
```

**Files added:**
- `/app/backend/shared/intents.py` — intent ingest + dry-run gate chain stub
- `/app/backend/shared/patches.py` — patch distribution + audit log
- `/app/runtime_patch_kit/decision_machine/decision_machine.py` — brain-side module
- `/app/runtime_patch_kit/decision_machine/DECISION_MACHINE_PATCH.md` — doctrine + how-to
- `/app/runtime_patch_kit/install_patch.sh` — bash installer with sha256 verification

**Doctrine:**
- Brains emit INTENTS, not orders. `may_execute=true` rejected at schema layer (422).
- `requires_gate_pass=true` schema-pinned. `seat_at_post_time` MC-stamped from live seat policy.
- Token-stack mismatch (alpha posting as camaro) returns 401.
- Patch distribution audit-logged in `shared_patch_pulls` (caller, patch, file, ts).
- Feature flag `DECISION_MACHINE_ENABLED` controls brain-side activation; flip to false = instant rollback.

**Verified end-to-end:** Camaro pulled the installer via curl-pipe-bash, both files written with sha256 match, `decision_machine.py` imports cleanly, audit log captured both pulls.

**New collections:**
- `shared_intents` — intent envelopes
- `shared_gate_results` — placeholder for Day 2 gate audit
- `shared_patch_pulls` — patch distribution audit

## 2026-02-13 — Route swap: public site to `/`, operator to `/admin`

Flipped the mount points so the consumer-facing RiseDual site is the root experience and the MC operator dashboard moved under `/admin/*`. Forward-compatible with the future `risedual.ai` DNS flip — no further URL changes needed.

**Routes after swap:**
- `/` → public RiseDual site (was `/r`)
- `/signals`, `/markets`, `/scanner`, `/heatmap`, `/activity`, `/digest`, `/chat`, `/signals/:id`
- `/r` and `/r/*` → 301 redirect to root (backward-compat for any bookmark)
- `/admin` → operator Overview (was `/`)
- `/admin/brain/:brain`, `/admin/promotion`, `/admin/discussion`, etc. — all operator paths re-prefixed
- `/login` — unchanged. Redirect after login: `/` → `/admin`.

**Files changed:**
- `App.js` — route table flipped
- `Layout.jsx` (operator) — `NAV` + `RUNTIMES` arrays re-pointed to `/admin/...`
- `Login.jsx` — post-login nav target → `/admin`
- `BrainConsole.jsx`, `RuntimeDetail.jsx`, `Redeye.jsx`, `Overview.jsx` — internal `<Link to>` and back-buttons updated
- All `risedual/**` pages — internal `/r/*` links rewritten to `/*`

**Verified live:** 7/7 swap tests pass — root renders public landing, `/r` redirects, `/admin` requires auth, login lands at `/admin`, `/admin/brain/camaro` renders console, `/signals` serves public page.

## 2026-02-13 — Brain Console pages (`/brain/:brain`)

User requested per-brain operator pages modeled after REDEYE's screenshot. Built one unified `BrainConsole.jsx` parameterized by brain name — same layout, different data per route.

**Routes shipped:**
- `/brain/alpha` · `/brain/camaro` · `/brain/chevelle` · `/brain/redeye`
- Sidebar `RUNTIMES` nav re-pointed from `/runtime/:r` + `/redeye` → `/brain/:b` uniformly
- Old routes (`/runtime/:runtime`, `/redeye`) kept for backward compatibility

**Sections per page:**
- Header (label, role, live pulse badge, reload)
- Mission Control Pulse — heartbeat age + sovereign contribution age + last seen + connection state
- Authority — promotion state + pending count + live-exec invariant
- Scorecard — total / wins / losses / win-rate from `/api/shared/scorecard`
- Conflicts — disagreements involving this brain from `/api/shared/conflicts`
- Discussion bus — last 10 opinions from this brain via `/api/shared/opinions`
- Speak as <brain> — admin proxy form (topic / stance / confidence / body)
- Pending approvals — promotion proposals filtered to this brain

**Backend addition:** `POST /api/admin/runtime-discussion/opinion` — admin-authed proxy that posts opinions as any brain without requiring the brain's runtime ingest token client-side. Stamps `posted_via=admin_proxy` + `posted_by_admin_email` in the audit trail.

**Files added:**
- `/app/frontend/src/pages/BrainConsole.jsx`

**Files changed:**
- `/app/backend/shared/opinions.py` — admin proxy endpoint
- `/app/frontend/src/App.js` — `/brain/:brain` route
- `/app/frontend/src/components/Layout.jsx` — sidebar nav re-pointed

**Verified live:** REDEYE shows 39 resolved trades, 51.3% win rate, 5 open AAPL conflicts, live discussion bus with ENDORSE/HYPOTHESIS opinions. Camaro shows active HOLD observation stream every 4-5s, speak-as form, pending challenger→advisor promotion.

## 2026-02-13 — VRL Doctrine Channel (read-only)

Mission Command now serves doctrine packets to all four brains via a read-only HTTP endpoint. First packet published: **Verified Reinforcement Layer (VRL)** — design-only doctrine for future morale/stabilization layer. No implementation yet, awareness only.

**New endpoint:**
- `GET /api/doctrine` — list available packets
- `GET /api/doctrine/{name}` — fetch full markdown for a packet
- Auth: existing `X-Runtime-Token` (any of the four brains' ingest tokens)
- Storage: `/app/runtime_patch_kit/*.md`, registry-gated so only whitelisted files are exposed

**Currently published:**
- `vrl` → `VRL_DOCTRINE.md` (6,125 bytes)
- `discussion_layer` → `DISCUSSION_LAYER_PATCH.md` (9,317 bytes)

**Verified live:** 401 on missing/bad token, 404 on unknown packet, 200 on valid runtime token for all four brains. Read-only — no `POST`/`PUT`/`DELETE`.

**Files added:**
- `/app/backend/shared/doctrine.py`
- `/app/runtime_patch_kit/VRL_DOCTRINE.md`

**Files changed:**
- `/app/backend/server.py` — mounted `doctrine_router`

## 2026-02-13 — Visual polish + candlestick charts (`/r/markets`)

User asked for: (1) softer palette, not so dark; (2) RISEDUAL all caps in logo; (3) candle charts for stocks and crypto. All shipped.

**Palette shift:**
- Bulk-replaced `bg-black` / `bg-zinc-9xx` / `border-zinc-9xx` → slate-based scale (`bg-slate-900` main, `bg-slate-800/40` cards, `border-slate-700`). Subtle navy tint, noticeably lighter and more "fintech" than pure black.

**Logo:**
- `RiseDual` → `RISEDUAL` (uppercase with `tracking-[0.18em]`, emerald `DUAL` accent preserved).

**Candlestick charts (new):**
- Backend: `GET /api/public/bars/{symbol:path}` returns OHLCV bars (newest-last, ascending). `GET /api/public/bars` lists all covered symbols grouped by tf/source.
- Frontend: `lightweight-charts@5.2.0` installed. `CandleChart` component renders candles + volume histogram with emerald/rose up-down coloring, interactive TF selector (1m/5m/15m/1H/4H/1D), pinned `localization.locale="en-US"` to dodge headless-browser locale crash.
- New page: `/r/markets` — symbol picker (Crypto / Stock / Other, ordered) + candle panel. Auto-selects first crypto pair on load.
- Embedded in `/r/signals/:id` under the header as "Price action".
- Nav updated: Home / Signals / **Markets** / Scanner / Heatmap / Activity / Digest / RiseDualGPT.

**Verified live:** BTC/USD on Kraken Pro renders 300 1H bars with last-price tag + volume bars; ETH/USD also wired.

## 2026-02-13 — Public Site Phase 2 (`/r/scanner`, `/r/heatmap`, `/r/activity`, `/r/signals/:id`)

Added the four remaining public surfaces on top of the MVP. Top nav now exposes Home / Signals / Scanner / Heatmap / Activity / Digest / RiseDualGPT.

**Routes shipped:**
- `/r/scanner` — 10 pattern-detection presets (MACD cross, Bollinger squeeze, EMA golden, volume spike, 52w extremes, RSI overbought/oversold, momentum breakout) with live match table.
- `/r/heatmap` — 24h % change grid (color-banded) + SPDR sector rotation rail. Gracefully degrades when feeders haven't accumulated 24h coverage.
- `/r/activity` — Live polled feed (10s) merging position audit / conflicts / outcomes into severity-tagged event cards. Pulse indicator in header.
- `/r/signals/:id` — Adversarial War Room (Bull / Bear / Commander) + Governance Pipeline (Strategist → Auditor → Synthesized) split. Signal cards on `/r/signals` now link here.

**Client changes:**
- `mc.js`: fixed scanner path (`/scanner/scan?preset_id=X`), agent-activity path (`/agent-activity/feed`), added `scannerPresets`, `sectors`, `signal` calls.
- `Signals.jsx`: signal cards now anchor to `/r/signals/:id` with emerald-hover border.

**Files added:**
- `src/risedual/pages/{Scanner,Heatmap,AgentActivity,SignalDetail}.jsx`

**Verification:** lint clean, compile clean, screenshot tested — signal detail renders header + War Room + Pipeline cleanly with live MC data; scanner shows preset list + scan progress; heatmap correctly degrades when feeders lack 24h coverage.

## 2026-02-13 — Public Site MVP (`/r/*`)

Built the consumer-facing `risedual.ai` surface inside MC's React app
(under `/app/frontend/src/risedual/`) so MC owns both backend AND
frontend for the public product. Alpha can be retired as site host when
DNS is flipped.

**Routes shipped:**
- `/r` — Landing (hero, council, features, CTA)
- `/r/signals` — Live signals + AI council consensus (`GET /api/public/signals`)
- `/r/digest` — LLM narrative + predictions table (`GET /api/public/digest/narrative`, `GET /api/public/digest`)
- `/r/chat` — RiseDualGPT chat panel, Pro Max gated (`POST /api/public/chat`)

**Implementation notes:**
- Distinct fintech aesthetic (dark, emerald accents, Chivo display font) — deliberately *not* the operator terminal look.
- Tier selector in header (Free / Starter / Pro / Pro Max) → drives `X-RiseDual-User-Tier` header. Persisted in localStorage as `risedual_site_tier`. Billing/auth stubbed.
- `X-RiseDual-Token` from `REACT_APP_RISEDUAL_TOKEN` (matches MC's `RISEDUAL_PUBLIC_TOKEN`).
- All elements have `data-testid` with `rd-*` prefix.
- Live API verified: consensus hero, signal cards, direction tags, narrative all render with real MC data.

**Files added:**
- `src/risedual/Layout.jsx`
- `src/risedual/context/TierContext.jsx`
- `src/risedual/lib/mc.js`
- `src/risedual/pages/{Landing,Signals,Digest,Chat}.jsx`
- `src/risedual/README.md`

**Files changed:**
- `src/App.js` — mounted `/r/*` route group
- `frontend/.env` — added `REACT_APP_RISEDUAL_TOKEN`

## 2026-02-13 — Unified Sidecar Convergence Patch shipped to brain agents

Delivered 3-block paste-ready patch (heartbeat loop / sovereign contribution loop / discussion-layer methods) to bring all 4 brains to fully-connected status. REDEYE's discussion layer now actively posting opinions to MC.

## 2026-02-13 — REDEYE Discussion Layer Unblocked

Clarified the dual-router quirk: opinions are **posted** to `/api/ingest/opinion` but **read** from `/api/runtime-discussion/opinions`. REDEYE now successfully posting (5+ opinions in 15 min after fix).

## Earlier (see PRD.md for full history)

- Public API Phase 1 + Phase 2 (signals, digest, chat, narrative, scanner, agent activity, models mind, heatmap) — DONE
- Public Traffic dashboard + per-tier rate limits — DONE
- Sovereign Sidecar Template + per-brain deployment bundles — DONE
- 62/62 backend pytest tests passing
