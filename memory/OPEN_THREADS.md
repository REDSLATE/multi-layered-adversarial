# Open threads — 2026-07-04 investigation

Session context: multi-round diagnosis of equity HOLD-collapse (07-02 06:00 UTC) that resolved
into three distinct root causes plus several defer-able side findings. Both closed and open
items tracked here so no assumption quietly becomes doctrine by default.

---

## 🔴 P0 — REOPENED as prod-side issue (operator confirmed data source)

### 1. "25→10/hr crypto emission drop" — CONFIRMED PRODUCTION

**Answered 2026-07-04 by operator:** the dashboard reporting the drop was reading
**production intents**, not preview or pooled `shared_intents`.

**Implication:** the entire preview-hibernation trace I did this session does NOT
explain the observed prod-side drop. That was noise from a different environment.
The prod-side crypto lane genuinely dropped emission rate ~60% in the observed
window, and this session did not touch that root cause.

**Blocked on:** I have no prod-pod observability from this preview container.
Cannot pull prod supervisor logs, prod backend logs, or prod-pod supervisor state.
Options:
  - Operator pulls prod backend logs for the relevant window (07-01 → 07-04 UTC)
    and shares the crypto-related lines (kraken, httpx, timeout, circuit, connection)
    for the same edge-of-silence-window analysis I did on preview.
  - OR operator triggers a diagnostic pull from prod (equivalent of the
    `bar_crypto.json` pull I ran, but hitting prod's `/api/intents` endpoint)
    so I can bucket prod's actual crypto emissions by hour and see the actual
    shape of the drop before speculating on cause.
  - OR the operator inspects prod-side pod lifecycle events directly — if prod
    is ALSO hibernating (unlikely for a production tier but possible), then
    this closes as platform-behavior on both sides.

**Do NOT assume the fix pattern from equity applies.** The equity investigation
resolved into a schema-drift bug in an enricher. The prod-side crypto drop could
be anything — a Kraken client circuit trip, a genuinely broken poll loop, a
per-symbol subscription drop, a memory leak causing partial-hangs, an intent
persistence bug swallowing writes silently. Fresh raw-data-first investigation
required.

**Recommended next step (when operator has bandwidth):** share a prod-side pull of
`GET /api/intents?stack=barracuda&lane=crypto&limit=500&sort=newest` — same query I
ran against preview earlier — plus a hint at which hours the drop was observed.
Enough to reopen the trace against real prod data.

---

## 🟡 P1 — awaits Monday market open

### 2. Equity spread enricher fix — Monday 07-06 13:35 UTC verification

**Status:** sign-off doc drafted at `/app/memory/SIGNOFF_equity_spread_enricher_field_drift.md`

**Binding gate:** cannot ship fixes #1/#2/#3 in that doc until Monday-market-open
verification passes. Verification command included in the sign-off doc's Section 0.

**Three outcomes to distinguish** (see doc Section 0):
  - Uniform pass across liquidity tiers → ship fix #1 as drafted
  - Liquidity-tiered pass → sub-review needed on threshold banding by liquidity tier
  - Uniform fail → field-name theory falsified, package needs rewrite

**Confounder awareness (§0b in the doc):** preview-pod hibernation can produce
false-negative results in the verification window if the pod is cold. Warm-hit
protocol documented in the doc.

**CFQS pre-fix-fires decision (§6a):** operator must choose Option A/B/C for how to
treat pre-fix fires against the 30-fire minimum. Doc will not proceed to code until
this is answered.

---

## 🟢 P2 — ready to ship / already shipped, low blast radius

### 3. Observation-receipts marooned under legacy names — SHIPPED

**Status:** IMPLEMENTED + MIGRATED + VERIFIED 2026-07-04

**Files changed:**
  - `backend/shared/observation_receipts.py` — endpoint at line 210 now canonicalizes
    the `brain` query parameter via `canonicalize_stack()`. Legacy names
    (alpha/camaro/chevelle/redeye) resolve to canonical names before DB filtering.
  - `backend/scripts/migrate_observation_receipts_legacy_brain_names.py` — one-shot
    idempotent migration script. Executed with `--apply`.

**Migration result:**
```
   alpha → camino         42 rows   ← updated 42
  camaro → barracuda    8511 rows   ← updated 8511
chevelle → hellcat         0 rows
  redeye → gto             0 rows
Total rows updated: 8553
```

Original brain names preserved in `brain_original_legacy` field for audit trail
and reversibility.

**Post-migration /counts endpoint output:**
```
  barracuda/crypto: total=1
  barracuda/equity: total=8510
  camino/equity:    total=42
```

**End-to-end verification (via live curl):**
  - `?brain=barracuda&lane=equity` → returns 2 rows (was 0 before). Rows carry
    `brain_original_legacy: "camaro"` — audit trail intact.
  - `?brain=camaro&lane=equity` → returns 2 rows via alias resolution (was HTTP 400
    before). Same underlying data.
  - `?brain=nonexistent&lane=equity` → still returns `HTTP 400 unknown brain`.
    Guard intact against genuinely-unknown names.

**Idempotence confirmed:** second run of migration script touches 0 rows.

**Rollback available:** restore legacy names via `brain_original_legacy` field
per §8 of `SIGNOFF_observation_receipts_legacy_names.md`.

### 3b. Webull token Mongo mirror — SHIPPED

**Status:** IMPLEMENTED + TESTED 2026-07-04

**File:** `/app/trader/webull_auth.py` — added `_mongo_collection()`, `_read_from_mongo()`,
`_write_to_mongo()`; hooked `_write_to_disk()` to also mirror to Mongo, hooked
`_read_from_disk()` to fall back to Mongo and rehydrate disk if file missing.

**Collection:** `webull_token` singleton doc (`_id="current"`) on the existing MongoDB.
Same instance the rest of the app uses. External-managed, survives pod redeploys.

**Test file:** `/app/backend/tests/test_webull_token_mongo_mirror.py` — 6 tests, all pass:
  - write-to-disk also mirrors to Mongo
  - post-redeploy simulation: file wipe + read → restores from Mongo AND rehydrates disk
  - both-empty state returns None cleanly
  - `get_token()` E2E works after simulated redeploy
  - writes are idempotent upsert (no doc accumulation on token refresh)
  - Mongo unreachable degrades gracefully to disk-only (pre-fix behavior floor)

**Operator workflow now:**
  1. One-time (per 15-day token TTL): run 2FA push via `/api/admin/trader/webull-token-create`
  2. Token gets written to both `/app/trader/data/webull_token.json` AND Mongo `webull_token` singleton
  3. On next redeploy: disk gets wiped; first read after boot pulls from Mongo and rehydrates disk
  4. No re-run of 2FA required until the 15-day server-side TTL expires

**Unblocks:** live-money trading continuity across deploys. Without this, every deploy
required a fresh 2FA push, making it structurally impossible to leave live trading
enabled through a code push.

---

## ⚪ P3 — filed for later, low impact

### 4. `sovereign_mode_guard` ImportError firing 5,372 times

**File:** `/app/external/brains/runner.py:1961` — `from shared.sovereign_mode_guard import ...`

**Impact:** background heartbeat loop, wrapped in try/except at line 1914. Does NOT
swallow intents. Effect is observability-only: MC's `sovereign_state.{brain}.updated_at`
goes stale → `STALE_SOVEREIGN` chip on `/api/admin/brain-emission/diagnose`. Log noise
of ~200-250 WARNING lines per hour.

**Fix options:**
  - (a) Restore `shared/sovereign_mode_guard.py` from git snapshot / `.revert_snapshots/`
  - (b) Wrap the import in try/except that silences after first failure to stop log spam

### 5. Missing `kraken_credentials` singleton in DB

**Source:** `trader/spread_stream.py` (dormant sidecar, `TRADER_ENABLED=false`).

**Impact:** noise only. Sidecar poller starts, fails to authenticate, logs warning, retries.
Does not touch live intent-emission path.

**Fix:** either insert the credentials doc OR gate the poller start on `TRADER_ENABLED`
to stop running when sidecar is disabled. Trivial either way.

### 6. Webull client re-initializing more than needed

**Symptom:** `_check_token_enable result is False` logged twice per snapshot call
(~88ms apart). Suggests `get_quotes_client()` is not memoizing the client handle
correctly across calls.

**Impact:** performance-only. ~1 extra HTTP round-trip per snapshot call for token-config
lookup. Not a correctness issue.

**Fix:** confirm client-cache logic in `webull_quotes.py` — should be a singleton or
module-level lazy init.

---

## ⚫ Filed as "not a bug, platform behavior"

### 7. Preview-environment pod hibernation

**Behavior:** Kubernetes evicting idle preview pods after ~30-90 min idle, cold-starting
on activity resumption. Produces 6-14 hour gaps in log stream + intent stream.

**Confirmed via:** `/var/log/supervisor/supervisord.log` — every silence window flagged in
the crypto investigation aligns exactly with a supervisor restart gap.

**Action:** none. Platform behavior. Documented in `SIGNOFF_equity_spread_enricher_field_drift.md`
§0b so future analysts don't misattribute hibernation gaps to code bugs.

---

## Closed with confidence this session

- Equity HOLD-collapse mechanism: `brain_core.py` spread-quality guard shipped ~07-03 03:00
  UTC and IS working on current intents (verified via 07-04 06:12 UTC fresh AAL BUY).
- `_check_token_enable = False`: red herring. Verified via SDK source. Gates token
  cache warm-up, not real-time entitlement.
- Argmax "tie-breaker" theory: superseded. `min_gap` per-brain per-lane already exists
  in `brain_tuning_cache.py`. Not the right fix layer even if it were needed.
- Reweighting-at-scoring-layer theory: superseded. Coordinated hold/observe boost was
  explained by market-quality inputs, not a scoring parameter change.
- External-brains architecture: not sidecars, in-process modules imported by
  `backend/routes/brain_runtime.py`.
