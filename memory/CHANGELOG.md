## 2026-06-09 (pass 2) — FIRST LIVE ORDERS HIT PUBLIC.COM · 5-layer gate diagnosis

### Operator confirmation (verbatim)
> "Yeah it's hitting the account"

### The 5 disguising layers
After flipping the learning ladder to `normal_live` (earlier this
day) the operator still saw no broker activity — only `dry_run_passed`
intents piling up. Walked the gate chain end-to-end and found that
each unblocked layer revealed the next:

1. **Learning ladder** — fixed earlier today (24 transitions).
2. **Executor seat (equity)** — Barracuda held it but only emitted
   HOLD. Position-model authority: only the *current* seat-holder's
   BUYs can route. Operator swapped executor↔auditor in the UI so
   GTO (already saturating BUYs on TSLA, MSFT) took the equity seat.
3. **Lane execution toggle** — already enabled, not the bottleneck.
4. **Auto-router asyncio task** — built a new status endpoint to
   confirm; task was alive & ticking every 30s.
5. **🎯 Master trading kill switch** (`/api/admin/trading/toggle`).
   Default state on pod first-boot is `enabled=False` (fail-closed
   safety). Never flipped on. Auto-router's Phase 1b check called
   `is_trading_enabled()` → False → persisted
   `no_trade: trading_controls_disabled` on every intent.

   Flipped True on both prod and preview at 14:32 UTC with reason
   *"operator green-light 2026-06-09: live pilot, ladder + lane +
   seat all open"*. First Public.com order landed within 60s.

### Files added this pass
- **Added**: `backend/routes/auto_router_admin.py` with two endpoints:
  - `GET /api/admin/auto-router/status` — task liveness + tick
    heartbeat counters. Cheap to poll, safe for the UI status strip.
  - `POST /api/admin/auto-router/force-tick` — drain queue
    immediately after unblocking a gate, no 30s wait.
- **Modified**: `backend/shared/auto_router.py` — added 6 module-level
  counters (`_STARTED_AT`, `_TICK_COUNT`, `_LAST_TICK_TS`,
  `_LAST_TICK_RESULTS`, `_LAST_TICK_EXECUTED`, `_LAST_TICK_ERROR`)
  populated by `_loop()` and exposed via `get_status()` /
  `force_one_tick()`. Module-level so reads cost nothing.
- **Modified**: `backend/server.py` — included `auto_router_admin_router`.

### Operational doctrine — the 12-point liveness chain
Every condition must be GREEN for autonomous order routing:

1. Brain emits `BUY/SELL/SHORT/COVER` intent with non-empty symbol
2. `gate_state` NOT in `{blocked, no_trade, advisory_only}`
3. Learning ladder `(brain, lane)` above `observation_only`
4. Executor seat for the lane filled (any brain)
5. Lane execution toggle = True
6. **Master trading switch = True**
7. Auto-router asyncio task = alive
8. `AUTO_ROUTER_ENABLED` env != false
9. Broker (Public/Kraken) connected + execution_enabled
10. Symbol in `patterns_universe` for the lane
11. RoadGuard spread floor: equity ≤ 50bps, crypto ≤ 200bps
12. Risk caps: per-order $25, per-day $50, open notional $200

### Master switch vs. lane toggle (the operator-facing distinction)
- Lane toggle = "I'm allowing routing on equity / crypto" (per-lane)
- Master switch = "I want autonomous trading happening RIGHT NOW"
  (single fleet-wide stop button)

Decoupled so flipping the master OFF requires no per-lane mutation
and flipping it back ON can't accidentally leave a lane disabled.

### Production deployment note
- Master switch flip (Mongo write) is **already live on prod** — no
  redeploy needed. Trades are firing.
- New `auto-router/status` and `auto-router/force-tick` endpoints
  are **preview-only** until prod is redeployed (Save to GitHub →
  prod redeploy pipeline). On prod today, the auto-router status is
  visible only via logs.

---

## 2026-06-09 — LIVE TRADING ENGAGED (ladder retired) + Public.com card + brain rename + Live Routes UI

### Operator instruction (verbatim)
> "I'm fine with real orders, I just had a month without any trades.
>  I need them to trade now. ... I just want them trading crypto and
>  equity. I think a month of intents makes up more than enough reason
>  for the ladder to go away."

### What shipped

**1. Ladder gate retired on PROD** — all 8 (brain, lane) combinations
flipped from `observation_only` → `normal_live` via the existing
`POST /api/admin/learning-ladder/promote` endpoint (3 calls per row,
24 total). Verified:
```
alpha     equity  normal_live      camaro    equity  normal_live
alpha     crypto  normal_live      camaro    crypto  normal_live
chevelle  equity  normal_live      redeye    equity  normal_live
chevelle  crypto  normal_live      redeye    crypto  normal_live
```
Each transition audit-logged with reason
`"operator decision 2026-06-09: ladder gate retired after 1 month of
observation_only intents. Per-order/per-day/open-notional caps now
serve as the binding risk control."`

**2. Equity broker card** — `AlpacaConnect` → `PublicConnect` on
`pages/Intents.jsx` Equity Lane section. New full-card layout
(`components/PublicConnect.jsx`) with the stat strip mirroring the
Kraken Crypto Lane tile: Account / Secret / Today $ / Open Notional /
Token Refresh. Connect form takes secret + optional account_id +
base_url + token_validity_minutes. ConnectedView surfaces test /
refresh-token / disconnect / execution-toggle. Execution toggle still
requires typed-phrase confirmation
(`I authorize execution on Public`).

**3. Brain display labels — back to Camino / Barracuda / Hellcat / GTO.**
Earlier in the session the operator-facing labels were briefly flipped
to Alpha/Camaro/Chevelle/Redeye (an inversion mistake on my part);
operator corrected: *"You have them backwards. Camino Barracuda Hellcat
and GTO are the new names."* Internal slot IDs remain
`alpha / camaro / chevelle / redeye` (Mongo primary keys — not
renamed, would require migration). Files updated to render the brand
labels everywhere:
- `external/brains/personality.py` — `BRAIN_PERSONALITIES` re-keyed
  to display Camino/Barracuda/Hellcat/GTO via `display_name` field
- `external/brains/runner.py` — `BRAIN_ROSTER` display column reverted
- `external/brains/brain_core.py` — class renamed
  `CaminoAdversarialBrain` → `NeutralAdversarialBrain` (already done
  in the rename direction; kept that way since the class no longer
  pretends to be one specific brand)
- `frontend/src/lib/api.js` — `RUNTIME_META` labels:
  `CAMINO / BARRACUDA / HELLCAT / GTO`; `roleTitle` field too
- `backend/.env` — `RISEDUAL_GIT_SHA="neutral-v3"` (version stamp
  rolled forward through both flips)
- Docstrings in `backend/routes/brain_runtime.py`, `backend/server.py`,
  `frontend/src/components/BrainProxiedStatusTile.jsx`
- `backend/tests/test_skills_and_personality.py` test using the new
  keys (the test was the only functional consumer of the legacy
  string keys; everything else was metadata).

Risk-profile multipliers preserved verbatim:
| Display   | Slot     | Mult  | Mode          |
|-----------|----------|-------|---------------|
| Camino    | alpha    | ×1.00 | balanced      |
| Barracuda | camaro   | ×1.15 | opportunistic |
| Hellcat   | chevelle | ×1.30 | aggressive    |
| GTO       | redeye   | ×0.85 | disciplined   |

61/61 backend tests pass post-rename.

**4. Live Routes admin page** — `frontend/src/pages/LearningLadder.jsx`,
mounted at `/admin/learning-ladder`, added to sidebar under Trading
as "Live Routes". Toggle-style grid:
- 4 brain cards × 2 lanes each (equity + crypto)
- Each lane row has 4 stage buttons (OBS / PAPER / LIVE / FULL)
- Clicking a non-current stage opens a modal that requires a
  reason (audit-logged) and shows a per-direction safety panel
  (live-execution warning when going up, safety-demote panel when
  going down)
- History card below lists the last 30 transitions with from-stage,
  to-stage, actor, reason
- Stage legend at the top of the page explains each rung

Backed by new endpoint:
- `POST /api/admin/learning-ladder/set` — direct stage selection
  (any rung from any rung) with audit row. Distinct from `/promote`
  and `/demote` which only step one rung. All 3 funnel through the
  same `_set_stage` write path so history reads uniformly.

**5. Confirmed prod & preview have separate Mongo DBs** for the
`learning_ladder` collection. The handoff's "shared Mongo" note was
not universally true. `public_credentials` and `kraken_credentials`
ARE shared (both envs see the same Public.com account 5LG34065 and
the same Kraken keys), but state collections like `learning_ladder`
are per-env. The Live Routes UI in preview won't reflect prod
ladder state until prod redeployed; ladder mutations against the
preview backend won't change prod brain routing.

**6. AAPL "phantom order" investigation — closed as real.** Operator
screenshot of Public.com Order History showed a real 0.0333 share
AAPL market buy via "Individual API" actor on Jun 09 02:09 AM —
the byproduct of a test/script run during the previous session.
Audit confirmed: no test, script, or repo path currently calls
`PublicAdapter.submit_market_order`, `route_order`, or
`/api/admin/public/order` outside the production gate path. So the
correction the previous agent applied appears solid — but the
historical fill DID happen.

### Files added or modified
- **Added**: `frontend/src/pages/LearningLadder.jsx`
- **Added**: `POST /api/admin/learning-ladder/set` (in
  `backend/shared/learning_ladder.py`)
- **Modified**: `frontend/src/App.js` (route),
  `frontend/src/components/Layout.jsx` (nav entry),
  `frontend/src/lib/api.js` (RUNTIME_META labels),
  `frontend/src/components/PublicConnect.jsx` (full card rewrite),
  `frontend/src/pages/Intents.jsx` (Alpaca → Public swap),
  `external/brains/personality.py`, `runner.py`, `brain_core.py`,
  `backend/.env`, `backend/routes/brain_runtime.py`,
  `backend/server.py`,
  `frontend/src/components/BrainProxiedStatusTile.jsx`,
  `backend/tests/test_skills_and_personality.py`.

### Production deployment status
Trading is live on prod **right now** — broker gates were already
open and the ladder was just flipped via prod's existing API. The
new UI surfaces (Public.com card on Intents, Live Routes page,
brand labels everywhere) require a **Save to GitHub → prod
redeploy** to land on `mission.risedual.ai`. Until then, prod
still shows the old Alpaca card / Alpha-Camaro-Chevelle-Redeye
labels in its UI — but the brains underneath are firing under the
new rules.

---


### Issue 1 — opinion loop dead
Operator screenshot showed `opinion: DEAD 0/1h`, `STALE_OPINION` badge
on all 4 brains, and a 3-9 day stale "last receipt" timestamp in the
runtime decision-log. Root cause: the in-process brain runner had
heartbeat / checkin / sovereign / intent loops but **no opinion loop** —
`shared_brain_opinions` was never being written to.

**Shipped:** `_post_directional_opinion` method on `BrainRunner`. Every
successful intent POST is followed by a directional opinion POST to
`/api/ingest/opinion`:
- `stance` = long / short / observation (derived from intent.action)
- `confidence` = intent.confidence (carries personality multiplier)
- `topic` = `symbol:<SYMBOL>`
- `may_execute=False` (descriptive evidence only)
- `evidence` carries the intent_id + personality_risk_mode

Verified live: 8 opinions in first 2 minutes after restart, all 4
brains posting, personality multipliers visible (GTO at 0.77 vs
Hellcat saturating at 1.0).

### Issue 2 — imposter scan showed preview check-ins on prod
Operator screenshot showed `DIVERGENT_ENV_NAME: ['preview', 'prod']`
on the imposter scan card on prod. Cause: preview and prod share
Mongo, so both pods' check-ins land in `sidecar_checkin_audit`.
Legitimate preview check-ins were flagging as imposters.

**Shipped:**
1. `GET /api/admin/runtime/sidecar-imposter-scan` now accepts `env`
   query param (default `all` preserves legacy behavior):
   - `env=prod` filters at the Mongo aggregation layer to only
     check-ins stamped `stamp_env_name=prod`
   - `env=preview` same, for preview-only inspection
   - `env=all` shows everything (debug cross-env confusion)
   - Response carries `env_filter` so the UI knows which mode is
     active
2. `ImposterScanCard.jsx` now has an `env` toggle (prod / preview /
   all) above the existing window toggle. Default = `prod` so the
   prod dashboard stops flagging legitimate preview check-ins.
3. 5 tripwire tests in `test_imposter_scan_env_filter.py`:
   - endpoint accepts `env` param
   - default is `all`
   - Mongo filter matches on `stamp_env_name` (not some other field)
   - response includes `env_filter` field
   - input is normalized (trim + lowercase)

### Verified live (preview)
```
GET /admin/runtime/sidecar-imposter-scan?env=prod   → 4 brains, all
                                                       envs=['prod'],
                                                       no imposter flags
GET /admin/runtime/sidecar-imposter-scan?env=all    → same (everything
                                                       currently stamps
                                                       prod via shared .env)
```

### Regression
67/67 tests pass in the full pre-launch cluster (imposter env,
intent origin, skills/personality, broker lane, brain runtime,
sovereign, identity stamp).

Loosened `test_route_order_allows_other_lane_when_one_disabled` —
Kraken is now CONNECTED in preview's Mongo, so an old assumption
that the test would error on missing creds no longer holds. Test
now confirms the lane toggle treats lanes INDEPENDENTLY (proves
the contract regardless of which downstream raises).

---


## 2026-02-XX (this session, pass 5) — Skills + personality layer, restriction-free

### Operator constraint
"Anything blocking trading is a nonstarter for me." Skills must be
LENSES/EVIDENCE, never gates. Every restriction lives in MC's
existing layer (lane toggles, ladder, sizing_gate, exposure caps,
MC receipt) — the operator controls these at runtime.

### Shipped
1. **Skills package** at `/app/external/skills/`:
   - `loader.py` — reads `SKILL.md` files with YAML frontmatter
     (`name`, `description`, `tags`), tolerant of bad files
     (skips with warning, never crashes runtime)
   - `selector.py` — tag-weighted scoring (tags 3x, description 1x),
     re-reads from disk each call so operator can hot-edit
     skills without restart
   - `skill_pack/`:
     - `crypto-execution/SKILL.md` — forms BUY/SELL/HOLD hypothesis
     - `adversarial-risk/SKILL.md` — counter-thesis as evidence
     - `risk-perception/SKILL.md` — risk vectors as evidence
     - `market-memory/SKILL.md` — history-informed conviction
   - ❌ **Removed `governor-risk/SKILL.md`** at operator request.
     Skills NEVER add gates on top of MC.
2. **`personality.py`** with `apply_personality_confidence` returning
   `(final, evidence)` tuple. Evidence dict:
   ```
   raw_confidence, personality_multiplier, personality_risk_mode,
   adjusted_pre_clamp, final_confidence, saturated_by_clamp,
   confidence_touched_by: ["personality.py", "math_clamp_0_1"]
   ```
   `saturated_by_clamp` flags honest 1.0 ceiling hits so audit shows
   when Hellcat/Barracuda's read genuinely wanted to push past 1.0.
3. **Personality multipliers** (operator-editable):
   - Camino    1.00× (balanced)
   - Barracuda 1.15× (opportunistic)
   - Hellcat   1.30× (aggressive)
   - GTO       0.85× (disciplined)
   Clamp at [0.0, 1.0] is math only (valid probability range), not
   a soft gate.
4. **Runner integration** in `_evaluate_and_post`:
   - Skill selector picks 3 skills based on (action, lane, symbol)
   - Personality multiplier applied to brain core's raw confidence
   - All three (skill names, skill evidence, confidence evidence)
     attached to the intent's `evidence` block
   - Intent still POSTs to MC's `/api/intents` via loopback —
     existing gates unchanged
5. **17 unit tests** in `test_skills_and_personality.py`:
   - clamp bounds, all 4 personalities distinct, evidence trail
     complete, saturation flagged honestly, unknown brain neutral
   - skill loader reads all 4 skills, governor-risk stays deleted,
     no HALT/force-HOLD/damp-by-X language in any skill body
   - selector tag-weight 3x description, runner imports + wires

### Verified live (preview)
Latest intent per brain in `shared_intents`:
```
Camino    : conf=1.000 raw=1.0 mult=1.00 saturated=False
Barracuda : conf=1.000 raw=1.0 mult=1.15 saturated=TRUE  ← honest ceiling
Hellcat   : conf=1.000 raw=1.0 mult=1.30 saturated=TRUE  ← honest ceiling
GTO       : conf=0.850 raw=1.0 mult=0.85 saturated=False ← honest dampener
skills_used: ['crypto-execution', 'risk-perception', 'market-memory']
```

### Doctrine pinned
- Skills enrich hypotheses; they NEVER gate.
- Personality multiplier modulates conviction; clamp is math only.
- Every touch on `confidence` is recorded in `confidence_evidence`.
- The only restriction layer is MC: lane toggles, ladder,
  sizing_gate, exposure caps, MC receipt. Operator controls each
  at runtime.

---


## 2026-02-XX (this session, pass 4) — Public.com wired live, Kraken pending, lane toggle shipped

### Shipped
1. **Public.com credentials connected** — operator's API secret stored Fernet-encrypted in `public_credentials`, access-token refresher running, account pinned to `5LG34065` (LEVEL_2 cash brokerage). Live API probe (`/portfolio/v2`) returns real positions.
2. **`execution_enabled=true`** flipped on Public.com via the audit-gated `/admin/public/execution` endpoint (confirmation phrase enforced).
3. **Funding state diagnosed**: $2,500 sits in the operator's HIGH_YIELD wallet (`5OT26003`), not the BROKERAGE account. HIGH_YIELD is `RESTRICTED_NO_TRADING`. Operator must move funds inside Public's UI before any equity order can fill.
4. **Operator lane toggle** (`/app/backend/routes/broker_lane_admin.py`):
   - `GET /api/admin/broker/lanes` — current per-lane enabled state
   - `POST /api/admin/broker/lanes/{lane}/toggle` — flip with confirm phrase
   - `GET /api/admin/broker/lanes/audit` — full toggle history
   - Confirm phrases: `"I authorize equity trading"` / `"Disable equity trading"`
   - Defaults: lanes ENABLED. Operator must explicitly disable.
   - **Wired into `broker_router.route_order`** at step 1b (BEFORE credentials probe) — disabled lane returns `BrokerRouteBlocked` with NO_TRADE.
   - **Fail-open** on Mongo blip: a toggle-lookup error logs warning and falls through to downstream gates (ladder, credentials, MC receipt). Lane toggle is operator override, not the safety default.
5. **New collections**: `broker_lane_toggles` (one row per lane), `broker_lane_audit_log` (append-only).
6. **9 unit tests** in `test_broker_lane_toggle.py`: defaults, explicit enable/disable, independence between lanes, KNOWN_LANES matches the broker registry, route_order blocks when disabled, route_order allows other lane when one is off, fail-open on lookup error.

### Verified live (preview)
- `GET /admin/broker/lanes` → equity + crypto both `enabled=true` by default
- `POST .../equity/toggle {enabled: false, confirm: "Disable equity trading"}` → flip persisted + audit row
- `POST .../equity/toggle {enabled: true, confirm: "I authorize equity trading"}` → flip back + audit row
- Audit log returns chronological history with actor email

### Doctrine
Lane toggle is the COARSEST gate — orthogonal to:
- `RISEDUAL_EQUITY_BROKER` (which broker for equity: Public vs Alpaca)
- Public/Kraken `execution_enabled` flags (per-broker kill switches)
- Learning-ladder stage (per-brain-per-lane)

Operator flips equity off → all 4 brains' equity intents NO_TRADE, ignoring everything below.

### Pending operator action
- **Public**: Move $2,500 from HIGH_YIELD (`5OT26003`) → BROKERAGE (`5LG34065`) in Public's app to unlock equity buying power.
- **Kraken**: Send API key + base64 private_key to wire crypto. Required scopes: Query Funds, Query Orders, Create/Modify/Cancel Orders. **NO Withdraw Funds scope.**

### Regression
50/50 tests pass in the broker_lane + brain_runtime + sovereign + identity + AV cluster.

---


## 2026-02-XX (this session, pass 3) — Brain identity stamp: canonical hash + fail-closed defaults

### Problem
Operator showed a screenshot from `mission.risedual.ai` where all 4
brains' check-ins displayed `ENV_NAME=preview`,
`MC_URL=https://multi-brain-backbone.emergent.host`,
`DB_NAME=multi-brain-backbone-test_database`, and a **HASH MISMATCH**
badge on every row. Two root causes:

1. `_checkin_stamp` in `external/brains/runner.py` hardcoded
   `"policy_hash": "neutral-template"` (a literal string) — that
   value could NEVER match MC's `sha256(canonical_policy_dict)`,
   so HASH MISMATCH fired on every check-in by construction.
2. The stamp sourced env identity from `BRAIN_ENV_NAME` (default
   `"prod"`) and `BRAIN_ADVERTISED_MC_URL` (default
   `https://mission.risedual.ai`). Defaults POSED as prod when the
   env var was missing — dangerous, since an unconfigured pod
   silently advertised "I'm prod" to the prod-readiness gate.

### Shipped
1. **`_checkin_stamp` now uses the canonical `policy_hash()`** from
   `shared.runtime.platform_survival` — the SAME SHA256 MC computes
   when validating. Matches by construction unless the doctrine
   dict actually diverges.
2. **Identity sourcing reordered + fail-closed defaults:**
   - `env_name`: `RISEDUAL_ENV` → `ENV` → `BRAIN_ENV_NAME` → `"unknown"`
   - `mc_url`: `RISEDUAL_MC_URL` → `BRAIN_ADVERTISED_MC_URL` → `""`
   - `git_sha`: `RISEDUAL_GIT_SHA` → `GIT_SHA` → platform-specific
     SHA vars → `BRAIN_GIT_SHA` → `"unknown"`
   - `db_name`: `RISEDUAL_DB_NAME` → `DB_NAME` → `""`
   - `broker_mode`: `RISEDUAL_BROKER_MODE` clamped to
     `{paper, live, dry_run}`; default `paper`
   - `platform`: `RISEDUAL_PLATFORM` → `PLATFORM` → `"emergent"`
   - `app_name`: `RISEDUAL_APP_NAME` → `"risedual"`
   - `sidecar_version`: `RISEDUAL_SIDECAR_VERSION` →
     `BRAIN_SIDECAR_VERSION` → `"neutral-camino-v1"`
3. **Identity startup log** — one line per process boot showing
   exactly what every check-in will stamp:
   `neutral_brain identity env_name=<v> mc_url=<v> db_name=<v>
    broker_mode=<v> git_sha=<v> — operator: this is what every
    brain check-in will stamp. If env_name != 'prod' or
    mc_url != 'https://mission.risedual.ai' on prod, set
    RISEDUAL_ENV / RISEDUAL_MC_URL in the prod deploy.`
   Operator can `tail -f` once after deploy to confirm.
4. **`tests/test_neutral_brain_identity_stamp.py`** — 13 tripwires
   covering: canonical hash match, RISEDUAL_* canonical source,
   legacy var fallback, fail-closed defaults, broker-mode clamp,
   full prod stamp passes validator, default stamp FAILS validator.

### Verified live (preview)
Latest `/api/admin/runtime/sidecar-checkin/alpha` shows:
```
policy_hash_match: true
mc_policy_hash:    2ac7d02164886f5c9c4a6339a605bf7be87b2bf2b532ea08681b5c29a6dcea25
stamp.policy_hash: 2ac7d02164886f5c9c4a6339a605bf7be87b2bf2b532ea08681b5c29a6dcea25  ✓
stamp.env_name:    preview      (correct for preview)
stamp.mc_url:      https://multi-brain-backbone.preview.emergentagent.com   (correct)
errors:            [ENV_NOT_PROD, MC_URL_NOT_PROD, UNKNOWN_GIT_SHA]   (correct for preview — these should ONLY clear on prod)
```
HASH MISMATCH is GONE. The remaining errors are honest signals
that this is a preview pod.

### Operator action — prod deploy needs these env vars
```
RISEDUAL_ENV=prod
RISEDUAL_MC_URL=https://mission.risedual.ai
RISEDUAL_DB_NAME=<prod database name (not "test_database")>
RISEDUAL_GIT_SHA=<actual commit SHA>
RISEDUAL_BROKER_MODE=paper          # or live / dry_run
RISEDUAL_PLATFORM=emergent
```
Once set + redeploy, the prod dashboard's identity check-in panel
will flip from `0 prod · 4 preview` to `4 prod · 0 preview`, all
"HASH MISMATCH" badges disappear, and the brain tiles drop their
`DEGRADED · no upstream` chips.

### Regression
76/76 tests pass across the regression cluster
(identity + sovereign + brain_runtime + alpha_vantage +
brain_emission_diagnose + sovereign_audit).

---


## 2026-02-XX (this session, pass 2) — Permanent brains: stripped dead external-sidecar proxy

### Problem
Operator reported: "A lot of those routes were used for the brains
no longer connected. It was to try and keep them connected but
failed more than succeeded." The `/api/admin/runtime/{brain}/status`
endpoint still proxied to external URLs configured via `{BRAIN}_STATUS_URL`
env vars. With those vars unset (and unable to be set — the external
sidecars are gone), every dashboard poll returned
`no_upstream_configured` and the BrainProxiedStatusTile rendered a
"MC could not fetch / set {BRAIN}_STATUS_URL" call-to-action. The
operator saw the brains as disconnected even though they're running
in-process.

### Shipped
1. **`/app/backend/routes/brain_runtime.py`** — full rewrite. Removed:
   - `_fetch_upstream` (httpx call to external sidecars)
   - `_upstream_url_for` + the `{BRAIN}_STATUS_URL` env-var contract
   - `_PROXY_CACHE` + `_cache_get/_cache_set` (TTL cache for the proxy)
   - `_write_proxy_audit` + `BRAIN_STATUS_PROXY_AUDIT` collection writes
   - `POST /admin/runtime/{brain}/status/refresh` (cache-bust for the dead proxy)
   - `GET /admin/runtime/status-proxy-audit` (forensics for the dead proxy)
   - `PROXY_TIMEOUT_S`, `PROXY_CACHE_TTL_S` env config
   The file shrank from 689 → 384 lines. Three live endpoints remain:
   `roster`, `{brain}/status` (in-process), `{brain}/universe`.
2. **`get_brain_status`** now serves directly from the in-process
   composer (`_build_in_process_status`). The response wrapper still
   uses the same shape (`brain, ok, _proxied_from, payload`) so the
   frontend tile is unchanged on success — only `_proxied_from` is
   pinned to `"in_process"` and the doctrine field reads
   `"in_process_runtime_status"`.
3. **`_build_in_process_status`** — composes status from:
   - `shared_heartbeats` (last_seen via the heartbeat reconciler)
   - `sovereign_state` (last contribution, mode, live_trading)
   - `shared_intents` (count_24h, count_1h, by-action breakdown,
     filtered on `stack` field — same as sidecar_diagnostics)
   - `shared.roster.get_roster()` (lane-resolved seats_held)
   - In-process `BrainRunner.stats` (tick/intent/checkin/sovereign counters)
   Payload sections (`identity`, `seats`, `heartbeat`, `intents`,
   `in_process_runner`) match what BrainProxiedStatusTile already
   renders.
4. **`/app/frontend/src/components/BrainProxiedStatusTile.jsx`** —
   removed the `useState`/`useCallback` force-refresh logic, the
   "↻ force-refresh" button, the "↻ retry" button, and the
   misleading `Set {BRAIN}_STATUS_URL ... redeploy MC` instructional
   text. The success-path renderer is preserved. Error path now
   shows a minimal "check backend logs for in_process_status_build_failed"
   banner (no dead-end CTA).
5. **`/app/backend/tests/test_brain_runtime.py`** — full rewrite.
   13 tripwires covering: roster lean-payload, brain-can't-peek,
   status endpoint operator-only, in-process marker, never-500,
   payload sections match the tile, **no httpx / no
   external-sidecar symbols reintroduced**, universe dual-auth +
   brain-pinned, broker keys never served, roster read-only,
   governor exclusivity isolation, exact router path inventory
   (live = 3 endpoints; dead = absent).

### Verified
- All 4 brains via `/api/admin/runtime/{brain}/status` return
  `ok=true, _proxied_from=in_process` with `heartbeat.alive=true`,
  `intents.last_24h > 350`, `intents.last_1h ≈ 90`. Heartbeats
  fresh (<25s) for every brain.
- Dead endpoints return 404:
  `POST /api/admin/runtime/alpha/status/refresh` → 404
  `GET /api/admin/runtime/status-proxy-audit` → 404
- 63/63 tests pass in the regression cluster (brain_runtime,
  neutral_brain_sovereign_loop, alpha_vantage_feeder, sovereign,
  brain_emission_diagnose, sovereign_audit).

### Doctrine pin
If external sidecars ever need to come back (they won't — the
brains are permanent), restore from git history. Do NOT bolt a
"future-proof" proxy onto `brain_runtime.py` — the tripwire
`test_status_endpoint_does_not_reach_for_external_sidecars` will
fail at the next test run.

---


## 2026-02-XX (this session) — Sovereign loop + Alpha Vantage cache for the 4 permanent brains

### Confirmation
The 4 neutral brains (Camino / Barracuda / Hellcat / GTO) are
**permanent**, not stand-ins. Treating them as first-class.

### Shipped
1. **`/app/external/brains/runner.py`** — added a 60s `_sovereign_loop`
   alongside the existing intent + checkin loops. Each brain now POSTs
   a substantive contribution (weights snapshot, rolling 20-decision
   tape, notes with tick/intent/last-action telemetry, mode=PRD)
   to `/api/runtime-discussion/sovereign/contribution` every minute.
   Cold-start delay of 8s + jitter so the first POST has at least one
   intent on the tape. Cadence tunable via
   `NEUTRAL_BRAIN_SOVEREIGN_SEC` (default 60).
2. **Rolling tape on every posted intent.** The runner now records
   the brain's last 25 POSTed decisions (`symbol/action/confidence/
   notional`) and ships the tail-20 in every sovereign contribution
   so MC's audit log carries real signal (not skeleton rows).
3. **`/api/admin/neutral-brains/status`** — added `sovereign_count`
   to the per-runner stats so the operator dashboard can show how
   many contributions each brain has posted since boot.
4. **`/app/backend/shared/feeders/alpha_vantage.py`** — new
   cached feeder for Alpha Vantage. Free-tier 25 calls/UTC-day cap.
   Cache row per `(symbol, function, date_utc)`; cache hits cost
   zero quota. AV rate-limit body (`Note` field) and explicit
   `Error Message` body are handled; rate-limit body pins the local
   counter to cap so we don't burn more quota in a hot loop.
   `force_refresh=True` bypasses cache. Background cache prune drops
   rows older than `ALPHA_VANTAGE_CACHE_RETENTION` (default 7d).
5. **`/api/admin/alpha-vantage/quota`**, **`/cache`**, **`/fetch`** —
   operator endpoints in `routes/alpha_vantage_admin.py`. Quota
   exposes used/cap/remaining/first/last; fetch is a manual probe
   that runs through the same cache+quota path every consumer uses.
6. **`namespaces.py`** — registered `ALPHA_VANTAGE_CACHE` and
   `ALPHA_VANTAGE_QUOTA` collections with doctrine pin.

### Verified
- 40 parallel `/api/auth/login` + 40 parallel `/api/health` calls
  all return 200, peak ~6s. The bcrypt `asyncio.to_thread` fix is
  confirmed protecting the event loop. (P0 — login 520 fix.)
- `GET /api/admin/brain/emission-diagnose/{brain}` for all 4 brains:
  `overall=LIVE` with `sovereign_loop: live` (was stale/dead before).
- `GET /api/admin/sovereign/state` shows fresh `updated_at` and
  substantive `notes`/`weights`/`recent_outcomes` for each brain.
- 5/5 new unit tests `test_neutral_brain_sovereign_loop.py` pass.
- 10/10 new unit tests `test_alpha_vantage_feeder.py` pass (cache
  hit, miss-with-quota-inc, quota exhausted, AV rate-limit body,
  AV error message, force-refresh, env-driven cap, prune, etc.).
- Full sovereign + brain_emission_diagnose suite: 50/50 green.

### Operator notes
- **Prod redeploy required** to pick up the bcrypt event-loop fix on
  `mission.risedual.ai`. Preview already has it.
- To lift the AV daily cap after upgrading tiers, set
  `ALPHA_VANTAGE_DAILY_CAP=<n>` in `backend/.env`. No code change.
- Consumers that want AV data MUST call
  `shared.feeders.alpha_vantage.get_payload(symbol, function)` —
  this is the SOLE egress to alphavantage.co.

---


## 2026-02-20 (pass #7) — CompositeLivenessCard frontend follow-up

### Problem
Operator confirmed the backend `composite_liveness` block ships
cleanly but the dashboard's old runtime table still rendered a
single heartbeat-driven badge. The "DEAD by heartbeat / passing
gate checks 45s ago" contradiction stays visually invisible until
the frontend reads the new field.

### Shipped
1. **`frontend/src/components/CompositeLivenessCard.jsx`** — new
   React card. Fans out `/admin/brain/emission-diagnose/{brain}`
   for all 4 brains in parallel, renders:
   - Per-brain column with overall band (LIVE / LIVE_DEGRADED /
     LIVE_IDLE / STALE / DEAD / NEVER) in a large badge
   - Reason-chip array (STALE_HEARTBEAT, DEAD_HEARTBEAT,
     STALE_SOVEREIGN, STALE_OPINION, ENGINE_ACTIVE)
   - Per-loop rows for all 6 loops (heartbeat / checkin / engine /
     directional / sovereign / opinion) with band + age
   - Auto-refreshes every 10 seconds
   - `data-testid` attributes on every operator-relevant element
2. **`frontend/src/pages/Diagnostics.jsx`** — slotted the new card
   directly above the legacy runtime table, wrapped in the standard
   `PanelErrorBoundary`. The legacy table still renders below so
   an operator with stale muscle memory has a fallback during
   rollout.

### Verified
- Live screenshot on preview shows the card rendering for all 4
  brains with chips, per-loop bands, and live refresh.
- Preview's `MC_EMIT_ENABLED=false` correctly produces DEAD/NEVER
  for every brain, proving the chip+band rendering works across
  every verdict band. Real impact lands on prod after redeploy.
- Lint clean on both files.

### Operator effect after prod redeploy
- The repeated "DEAD ↔ LIVE" flip-flop on Chevelle/RedEye should
  stop being misleading. The card honors "engine alive = brain
  alive" via the LIVE_DEGRADED verdict.
- Per-loop bands let an operator see WHICH loop is wedged in one
  glance — no more "is it a heartbeat issue or a sovereign issue?"
  curl rounds.
- Existing legacy table remains visible below; can be removed in
  a follow-up pass once the operator confirms the new card meets
  every diagnostic need.


## 2026-02-20 (pass #6) — Composite per-loop liveness

### Problem (operator-caught)
Operator screenshots showed REDEYE marked `DEAD 308s` by the
heartbeat-driven badge while a different panel showed RedEye
actively passing gate checks 45s ago. The badge was lying because
it collapsed all signals into one heartbeat-driven status, hiding
the real failure mode: one loop in the brain can die while others
stay healthy. The operator named the fix exactly right —
"composite liveness, not brain-level liveness."

### Doctrine pin (2026-02-20)
MC's brain status is now a composite of independent loop signals:
  - heartbeat_loop   — shared_heartbeats.last_seen
  - checkin_loop     — sidecar_checkin_audit.ts
  - engine_loop      — shared_intents.ingest_ts (any action)
  - directional_loop — shared_intents (BUY/SELL/SHORT/COVER only)
  - sovereign_loop   — sovereign_state.{brain}.updated_at
  - opinion_loop     — shared_brain_opinions count

Each loop gets its own band (live/stale/dead/never).
Overall verdict respects "engine alive = brain alive":
  LIVE          — heartbeat fresh
  LIVE_DEGRADED — heartbeat stale/dead BUT engine OR directional fresh
                  (the REDEYE pattern — no longer DEAD)
  LIVE_IDLE     — heartbeat fresh, but quiet on engine + directional
  STALE         — heartbeat stale, no engine signal
  DEAD          — heartbeat dead AND engine stale AND no directional
  NEVER         — brain never contacted MC

Reason chips for the UI to render as badges:
  STALE_HEARTBEAT, DEAD_HEARTBEAT, STALE_SOVEREIGN,
  STALE_OPINION, ENGINE_ACTIVE.

### Shipped
1. **`routes/brain_emission_diagnose.py`** — new `_composite_liveness`
   helper. Pure function of the signals MC already collects. Zero
   new collections, zero new writes.
2. **`_diagnose_one`** now returns `composite_liveness` block alongside
   the existing `heartbeat`, `sidecar_checkin`, `roster`, `emission`.
3. **`tests/test_composite_liveness.py`** — 8 tests:
   - Source tripwire (helper exists + all 6 loops + all 6 verdicts)
   - Six verdict-derivation cases pinned (incl. the exact REDEYE
     symptom: heartbeat dead + engine fresh ⇒ LIVE_DEGRADED + ENGINE_ACTIVE)
   - End-to-end endpoint shape

### Verified
- Live curl against all 4 brains on preview shows the new
  `composite_liveness` block with per-loop bands rendered.
- Preview correctly shows DEAD/NEVER for all (MC_EMIT_ENABLED=false
  is intentional in preview) — proving the helper distinguishes the
  bands cleanly. The real impact lands on prod after redeploy.
- 54/54 focused tests green (8 new + 46 prior across the day's
  passes).

### Operator impact on prod after redeploy
- The Diagnostics dashboard can now render per-loop chips (e.g.
  "REDEYE · LIVE_DEGRADED · STALE_HEARTBEAT · STALE_SOVEREIGN ·
  ENGINE_ACTIVE") instead of one misleading "DEAD" badge.
- The repeated up/down/up confusion stops — the badge no longer
  flips between LIVE and DEAD on heartbeat oscillation if the
  engine is firing.
- The exact failed loop is surfaced as a chip, so the next debug
  step is one click away ("STALE_SOVEREIGN → go look at sovereign
  contributions").

### Frontend follow-up
This pass updates only the backend. The Diagnostics page's badge
currently reads `heartbeat_age_seconds` to decide LIVE/STALE/DEAD;
that path can keep working as the fallback. A follow-up pass should
update `pages/Diagnostics.jsx` to read `composite_liveness.overall`
+ `composite_liveness.chips` and render the chip array.


## 2026-02-20 (pass #5) — Feeder auth 401 error-message upgrade

### Problem
REDEYE's agent took a multi-step round-trip ("is this token wrong?
is the source wrong? do I need a separate REDEYE_FEEDER_TOKEN?")
to land on the actual answer: the OHLCV endpoint auth is
source-keyed (`source: "kraken_pro"` → `KRAKEN_FEEDER_TOKEN`),
not brain-keyed. The bare `"invalid feeder token"` 401 gave them
nothing to work with.

### Shipped
`shared/technicals.py:_verify_feeder` rewritten with informative
error messages that name the expected `env_key`:

- **400 (unknown source)**: lists allowed sources
- **401 (missing token)**: `"missing X-Feeder-Token header (source='kraken_pro' expects env_key='KRAKEN_FEEDER_TOKEN')"`
- **401 (wrong token)**: `"invalid feeder token (source='kraken_pro' expects env_key='KRAKEN_FEEDER_TOKEN'; compare your X-Feeder-Token against the MC deploy's 'KRAKEN_FEEDER_TOKEN' env var)"`
- **503 (env var unset on MC)**: `"feeder token for source='kraken_pro' is not configured on MC (env_key='KRAKEN_FEEDER_TOKEN' is empty/missing on this deploy)"`

The `env_key` NAME is public info (anyone reading the source code
sees it in the FEEDERS dict). The token VALUE is never echoed —
not from MC's env, not from the caller's header. Tests pin both
properties.

### Verified
- Live curl: posting wrong token now returns the full informative
  message above.
- 5 new tests in `test_feeder_auth_errors.py` pin all four error
  paths AND the no-token-value-echo invariant.
- 27 passed / 2 skipped across affected suites.

### Doctrine note for the operator
The token VALUE is still secret and lives in MC's env vars per
deploy. The operator (or platform admin) is the only party that
should have it. The error message tells callers EXACTLY which env
key to look up — but they have to find the value themselves via
their hosting platform's secrets management.


## 2026-02-20 (pass #4) — Heartbeat reconciler worker

### Problem
Operator screenshot 2026-06-03 showed REDEYE simultaneously:
  - SIDECAR IMPOSTER SCAN: 19 check-ins in last 1h, all clean prod
  - HEARTBEAT STATUS: DEAD 375s
Most likely cause: REDEYE pod genuinely went silent in the last
few minutes (the 19 check-ins happened earlier in the hour window).
But there's a real durability gap that could cause an identical
symptom on another brain: the per-request heartbeat side-effect
in `shared/runtime/sidecar_checkin.py` is wrapped in try/except,
so a transient Mongo write blip silently swallows the heartbeat
bump while the `sidecar_checkin_audit` row above DID persist.

### Doctrine pin
Belt-and-suspenders: per-request side-effect remains the canonical
fast path. A background reconciler closes the durability gap by
deriving `shared_heartbeats.last_seen` from `sidecar_checkin_audit`
on a 60s tick. Advisory observability only — never reassigns a
seat, never gates execution, never overwrites a fresher heartbeat.

### Shipped
1. **`shared/runtime/heartbeat_reconciler.py`** — new worker module
   mirroring the `opinion_silence_worker` pattern:
   - `perform_reconcile(max_age_sec)` — pure function-of-DB-state, callable from worker loop OR an admin endpoint
   - For each brain in DISCUSSION_PARTICIPANTS: find latest
     `sidecar_checkin_audit.ts`, compare to current heartbeat,
     upsert if audit is strictly newer
   - Refuses to bump from audit rows older than `max_age_sec`
     (default 30 min) — no "rewrite history" failure mode
   - Bumped rows carry `detail.source = "heartbeat_reconciler"`
     so operators can tell reconciled bumps from real pings
   - Config: `HEARTBEAT_RECONCILER_ENABLED` (true), `TICK_SEC`
     (60s), `MAX_AGE_S` (1800s)
2. **`routes/heartbeat_reconciler_admin.py`** — operator endpoints:
   - `POST /api/admin/heartbeat-reconcile/run` — manual trigger,
     returns the same summary the worker logs
   - `GET /api/admin/heartbeat-reconcile/status` — config view
3. **`server.py`** — starts the worker on boot, wires the admin router
4. **`tests/test_heartbeat_reconciler.py`** — 7 tests:
   - Source tripwires (helper exists + wired into boot + admin router included)
   - Behavioral: bumps when audit newer, refuses ancient audit
     rows, no-op when heartbeat already fresh, lists no-audit brains
   - End-to-end: admin endpoints return correct shape

### Verified
- Boot log: `heartbeat_reconciler started: tick=60s max_age=1800s`
- `GET /admin/heartbeat-reconcile/status` returns enabled=true
- `POST /admin/heartbeat-reconcile/run` returns full summary with
  bumped/no_change/skipped_stale/no_audit breakdowns
- 31/31 focused tests green (7 new + 24 prior across the day)

### Operator effect on prod after deploy
- Within 60s of any sidecar check-in that lands but fails its
  heartbeat side-effect, the reconciler will retroactively bump
  the heartbeat row. The LIVE/STALE/DEAD badge can't drift out
  of sync with the Imposter Scan for more than 60s now.
- A brain that genuinely goes silent (the most likely REDEYE
  cause) will still correctly age out to DEAD — reconciler only
  refreshes when the audit log proves the pod is alive.
- Manual `POST /admin/heartbeat-reconcile/run` available for
  post-deploy investigation.


## 2026-02-20 (pass #3) — `last_ohlcv_push_success_at` + OHLCV 422 diagnostic

### Cross-team coordination (REDEYE → MC)
REDEYE's agent reported their OHLCV pushes were returning 422 across
7/7 symbols with the diagnosis "MC validator expects top-level
body.ts but REDEYE batches ts inside each bars[*]." Asked MC to fix
the validator OR REDEYE would flatten to 1-bar/POST.

### Reproducible finding
Live preview reproduction showed the diagnosis was wrong-shaped.
MC's actual schema (`shared/technicals.py:131-133`):

```python
class OHLCVBatchIn(BaseModel):
    bars: list[OHLCVBarIn] = Field(..., min_length=1, max_length=2000)
```

— accepts the exact wire shape REDEYE described. Posting the batch
envelope to `/api/ingest/ohlcv/batch` returns 200 + persists bars.
The 422 with `loc: ["body","ts"]` is what Pydantic returns when
you POST the batch envelope to the **single-bar URL**
`/api/ingest/ohlcv` (no `/batch` suffix). 8 errors are returned,
of which `body.ts` is the 4th; the brain agent fixated on that one
without noticing the other 7 (body.source, body.symbol, body.tf,
body.o, body.h, body.l, body.c) all required at top-level too.

**Root cause: REDEYE is POSTing to the single-bar URL with batch
envelopes. Fix is REDEYE-side: change URL to `/api/ingest/ohlcv/batch`.**

Full diagnostic + paste-ready response written to
`/app/memory/MC_RESPONSE_TO_REDEYE_OHLCV_422.md`.

### Shipped (parallel field per their proposal)
- **`LoopStatus.last_ohlcv_push_success_at`** added as optional ISO
  8601 field. Brains populate it whenever an OHLCV push returns
  2xx. MC dashboard will surface "sidecar healthy, sovereign
  healthy, OHLCV silent" patterns at a glance, closing the same
  failure-mode that REDEYE's 422 storm exposed.
- Backward-compat (defaults to None). Existing brains keep working.
- 15 tests still green (no test changes needed — the new field is
  passed through `model_dump()` and persisted automatically).

### Operator next step
Forward `/app/memory/MC_RESPONSE_TO_REDEYE_OHLCV_422.md` to REDEYE
team. If they confirm Option 1 (URL fix), zero MC changes needed.
If they're already on `/batch` and still 422, paste the full curl
reproduction back so MC can chase a possible deploy mismatch.


## 2026-02-20 (pass #2) — Sidecar check-in `loop_status` extension

### Cross-team coordination
The RedEye brain team's agent flagged a real semantic gap on MC:
sidecar identity check-ins were firing cleanly (19/hr, prod-verdict,
clean imposter scan) while sovereign contributions had been silent
for 3 days. The Diagnostics dashboard simultaneously showed
"LIVE 11s" (heartbeat) and "last receipt 3d ago" (sovereign) —
two contradictory truths.

Both signals are correct as far as they go; they measure different
loops. The brain team proposed enriching the sidecar check-in
payload with last-activity timestamps so MC can notarize all of
the brain's internal loops on every check-in, surfacing the
inconsistency in one glance.

### Doctrine pin
The brain attests to its own internal loop freshness on every
check-in. MC notarizes the attestation and derives an operator-
facing `loop_health` band. Backward-compat: brains that don't
ship the extension keep working; their `loop_health` defaults
to `unknown`.

### Shipped
1. **`shared/runtime/sidecar_checkin.py`** — new `LoopStatus`
   Pydantic schema with six optional fields:
   `last_decision_log_at`, `last_opinion_at`, `last_intent_at`,
   `last_sovereign_contribution_at`, `tick_loop_healthy`,
   `tick_loop_last_error`. All ISO 8601 UTC; `tick_loop_last_error`
   capped at 1000 chars.
2. **`CheckinRequest`** extended with `loop_status: Optional[LoopStatus]`.
   Existing brains keep working with no change.
3. **POST handler** persists `loop_status` (raw) and `loop_health`
   (derived band) into `sidecar_checkins.{runtime}`.
4. **`_loop_health_from(...)`** band derivation helper:
   - `unknown` — no block, empty block, or no sovereign timestamp
     yet (silence is not implicit failure)
   - `green` — sovereign < 1h, `tick_loop_healthy != False`
   - `amber` — sovereign 1h-6h
   - `red` — `tick_loop_healthy: false`, sovereign > 6h, or
     malformed timestamp
5. **`routes/brain_emission_diagnose.py`** — sidecar_checkin block
   now surfaces `loop_status` (raw) and `loop_health` (band)
   alongside the existing identity verdict.
6. **`tests/test_sidecar_loop_status.py`** — 5 tests:
   - Source tripwires (schema field, persistence, diagnose surface)
   - Unit: band derivation across 6 documented cases
   - Behavioral: round-trip POST → diagnose with both `green` and
     `unknown` paths

### Verified
- Live curl: POSTed fresh+healthy loop_status → MC returned
  `ok: True verdict: prod` → emission-diagnose returned
  `loop_health: green` with all 4 timestamps intact.
- 29 passed / 2 skipped (Alpha token unset in this env so
  end-to-end POST tests skip gracefully).

### Brain team contract handoff
```json
POST /api/admin/runtime/sidecar-checkin/{brain}
Header: X-Runtime-Token: <per-brain token>
Body:
{
  "stamp": { ... existing fields, unchanged ... },
  "loop_status": {
    "last_decision_log_at":             "2026-06-03T03:42:11Z",
    "last_opinion_at":                  "2026-06-03T03:42:08Z",
    "last_intent_at":                   "2026-06-03T02:14:00Z",
    "last_sovereign_contribution_at":   "2026-05-30T12:27:18Z",
    "tick_loop_healthy":                true,
    "tick_loop_last_error":             null
  }
}
```

Brain teams may opt in incrementally — ship `loop_status` with
just `tick_loop_healthy` first, then add timestamps as they wire
each loop's instrumentation.


## 2026-02-20 — Boot-time legacy doc reconcile + crypto universe expansion + no-op-assign wipe

### Problems caught from production
1. **Drift banner persisted after deploy** — the auto-wipe shipped
   yesterday only fires on roster WRITES, not on boot. A deploy
   into prod where the legacy `shared_executor_seat` doc already
   held a stale value kept the banner firing until the operator
   manually triggered a write.
2. **No-op roster writes skipped the wipe** — clicking the same
   brain pill on Quick Seat Switches (operator's intuitive "refresh
   state" gesture) hit the `new_assignments == prev` early return,
   never reaching the auto-wipe call site. Operator's escape hatch
   didn't work.
3. **Universe gate rejected real crypto signal** — Alpha (now in
   crypto_strategist seat) was producing decision logs across 5
   crypto pairs (AVAX, LINK, ADA, BNB, XRP), but only XRP was in
   the seeded universe. The other 4 would have been rejected at
   the gate the moment Alpha tried to emit a routable intent.

### Shipped
1. **`server.py` boot reconcile**: on app startup, compare
   `shared_brain_roster.assignments.executor` with
   `shared_executor_seat.holder`. If they disagree and the roster
   has a non-null executor, clear the legacy doc. Logs the action
   so an operator can see what happened. Idempotent (re-running is
   a no-op).
2. **`server.py` crypto seed extended**: added `AVAX/USD`,
   `LINK/USD`, `ADA/USD`, `BNB/USD` to the boot seed alongside
   the original 4 majors. `$setOnInsert` semantics — won't
   overwrite operator-edited rows.
3. **`shared/roster.py` no-op-aware wipe**: when `/assign` hits
   the `new_assignments == prev` early return AND `target_role ==
   "executor"`, still fire `_wipe_legacy_executor_doc`. This makes
   the "click the same pill again" operator escape hatch actually
   work.

### Verified
- Restart with seeded stale `alpha` legacy doc → boot reconcile
  logged `cleared legacy shared_executor_seat (was 'alpha',
  roster.executor='redeye')`.
- Restart with consistent doc → boot reconcile logged
  `legacy executor doc consistent with roster — no wipe needed`.
- `patterns_universe seeded (8 equity + 8 crypto)` — all four new
  pairs present.
- 26/26 tests green (including the previously-failing
  re-assign-same-brain wipe test).

### Operator effect on next prod deploy
- The drift banner will clear automatically at boot.
- No manual `/api/executor/rotate` curl needed post-deploy.
- AVAX/LINK/ADA/BNB intents from Alpha will route normally
  (assuming gate-chain passes and Kraken accepts them).
- Operator's "click the same pill" muscle memory now works as
  expected.


## 2026-02-19 (pass #3) — Legacy executor doc auto-wipe on roster writes

### Problem
Operator screenshot: "SEAT REGISTRY DRIFT DETECTED — seat executor —
roster says redeye, legacy doc says alpha, gate sees redeye" banner
firing on the Intents page after every Quick Seat Switch. The
gate was correct (reads from roster, prefers redeye), but the
legacy `shared_executor_seat` doc held the stale `alpha` value
from a pre-QSS rotation. Two storage locations, no auto-sync,
operator had to manually `POST /api/executor/rotate` after every
roster change to silence the banner.

### Doctrine pin
The roster (`shared_brain_roster.assignments`) is the single source
of truth for seat ownership. The legacy doc is fallback-only. The
gate already prefers roster. Now the legacy doc auto-clears any
time the roster writes the executor seat — drift can't accumulate.

### Shipped
1. **`shared/roster.py`** — new `_wipe_legacy_executor_doc(actor,
   reason)` helper. Writes `holder=null, since=null, reason=
   "auto-cleared by roster write (...)"` to
   `shared_executor_seat`. Best-effort (try/except) so a write
   failure can't block the roster assignment.
2. **`shared/roster.py`** — three call sites added:
   - `/assign` — fires when `target_role == "executor"` OR when
     the same-lane vacate side-effect changes the executor holder.
   - `/swap` — fires when either swapped role is `executor` OR
     when the executor holder changes.
   - `/reset` — always fires (reset writes a full default roster).
3. **`tests/test_legacy_executor_auto_wipe.py`** — 2 tests:
   - Source tripwire: helper exists + wired into 3 paths (4 occurrences total).
   - Behavioral: seed legacy doc to `alpha` → roster-assign executor
     to `redeye` → assert legacy doc is now `null`.

### Verified
- Live curl: seeded legacy doc to `alpha`, ran
  `/api/admin/roster/assign role=executor brain=redeye`, then
  `/api/admin/seat-registry/diagnose` returned
  `legacy_executor_seat_doc.holder = null`, `reason = "auto-cleared
  by roster write (roster assign executor=redeye)"`,
  `gate_view.executor.source = "roster"`.
- 26/26 focused tests green (2 new + 24 prior across all today's
  passes: heartbeat + universe + auto-wipe).

### Operator effect
After this is deployed to prod:
- The "SEAT REGISTRY DRIFT DETECTED" banner will never fire from
  roster-assign / swap / reset writes again.
- Operator can use Quick Seat Switches freely — no manual
  `/api/executor/rotate` follow-up needed.
- The legacy `shared_executor_seat` collection still exists for
  back-compat with any callers that still POST to
  `/api/executor/rotate` directly. Those callers continue to work
  but their writes will be overwritten the next time the roster
  is touched. Doctrine-clean: roster wins, every time.


## 2026-02-19 (pass #2) — Symbol-in-universe gate + brain-callable universe endpoint

### Operator pin
Camaro has been emitting equity-only intents despite holding the
crypto-strategist seat. Root cause confirmed via prod query:
`GET /api/admin/intents?stack=camaro&lane=crypto` returned empty —
Camaro has never proposed a crypto trade. This pass closes the
underlying architectural gap: MC had no central control of what
symbols brains were allowed to propose against. Brains used their
own hardcoded universes, drifting silently.

### Doctrine (c) pin
MC verifies boundaries, brains propose, MC routes. Adding
"symbol-in-universe" to the gate chain plugs the last big un-MC'd
boundary. A brain can now hard-code whatever it wants — its
off-universe intents will be rejected at MC's gate chain with
`symbol_in_universe: passed=False`. One operator curl can add or
remove a tradeable symbol fleet-wide.

### Shipped
1. **`shared/execution.py`** — new `symbol_in_universe` gate
   (gate 5c, between `broker_connected` and `lane_execution_enabled`).
   Looks up `intent.symbol` in `patterns_universe` with `active:
   {$ne: false}`. Enforces:
   - Off-universe symbol → `passed=False`, reason names the symbol +
     gives the curl command to add it.
   - Wrong-lane (symbol exists but tagged equity, intent is crypto) →
     `passed=False`.
   - Lane-untagged intent (legacy) → accepted against any universe
     lane (preserves equity-bootstrap path; broker_connected gate
     above forces correct broker).
   - Match → `passed=True`.
2. **`routes/data_stack_admin.py`** — `UniverseSymbolIn` schema
   extended with `lane: str = "equity"` field; rejects any value
   outside `{equity, crypto}` with 422. POST writes `lane` onto the
   row.
3. **`server.py`** — boot seed extended:
   - Existing 8 equity tickers get `$set: {lane: "equity"}` (idempotent
     backfill on any pre-existing rows).
   - New crypto seed: `BTC/USD`, `ETH/USD`, `SOL/USD`, `XRP/USD` with
     `lane=crypto`. Same `$setOnInsert` pattern so reseeding doesn't
     reset operator-edited rows.
4. **`routes/brain_runtime.py`** — new
   `GET /api/admin/runtime/{brain}/universe` endpoint:
   - Dual auth (operator JWT OR `X-Brain-Id` + `X-Runtime-Token`).
   - Brain-auth enforces brain-id-matches-path (no cross-brain peek).
   - Filters universe by the brain's currently-held seats →
     resolved lanes.
   - Returns `{symbols: [{symbol, lane}], lanes: [...], count, served_at}`.
   - Backward-compat: rows without `lane` are treated as equity.
5. **`memory/brain_universe_client_reference.py`** — drop-in Python
   client brain teams copy into their repo. Async, lane-filtered
   query API, in-memory cache with `MAX_CACHE_AGE_SEC = 6h` safe-mode
   fallback, `/status` snapshot for operator visibility. Documents
   the doctrine: "only valid reason to skip MC is a failure code."
6. **`tests/test_symbol_in_universe_gate.py`** — 7 tests:
   - Source tripwires (gate exists in chain, endpoint exists).
   - Behavioral: crypto pairs seed on boot.
   - Universe endpoint shape, unknown-brain 404, invalid lane 422.

### Verified
- Boot log: `patterns_universe seeded (8 equity + 4 crypto)`.
- `GET /api/admin/patterns/universe` shows all 12 rows with explicit
  `lane` field.
- `GET /api/admin/runtime/alpha/universe` returns 8 equity symbols
  filtered to alpha's seat (executor → equity lane).
- POST with `lane: "metals"` → 422.
- 24/24 focused tests green (7 new + 17 prior heartbeat tests).

### Rollout for brain teams
The MC side is live. Brain teams need to:
1. Copy `/app/memory/brain_universe_client_reference.py` into their
   brain repo as `brain_universe_client.py`.
2. In their strategist init: instantiate `BrainUniverseClient(...)`,
   call `await client.start()`.
3. Replace any hardcoded symbol list with `client.allowed_symbols(lane=...)`.
4. When emitting intent, set `lane` to match the symbol's lane from
   the client.
5. Deploy.

Until they do this, MC's new gate will REJECT off-universe intents.
For Camaro specifically, this means equity intents will keep failing
gates until Camaro's strategist learns to query
`/admin/runtime/camaro/universe` and see crypto pairs in its
candidate set.

### Operator one-curl examples
```bash
# Add a symbol fleet-wide
curl -X POST $MC/api/admin/patterns/universe \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"COIN","lane":"equity","note":"crypto proxy"}'

# Soft-deactivate (no longer tradeable, audit row preserved)
curl -X DELETE $MC/api/admin/patterns/universe/HOTH \
  -H "Authorization: Bearer $TOKEN"

# Brain-side view (what camaro is allowed to propose right now)
curl $MC/api/admin/runtime/camaro/universe \
  -H "Authorization: Bearer $TOKEN"
```


## 2026-02-19 — Heartbeat side-effect on sidecar check-in + raised STALE/DEAD bands

### Operator pin
RedEye was showing DEAD on the runtime liveness table while the
Sidecar Imposter Scan showed 21 clean check-ins/hour for it. The
two widgets read from different collections (`shared_heartbeats` vs
`sidecar_checkins`) and never crossed — so a brain whose sidecar
only POSTed to `/sidecar-checkin/{brain}` would appear dead even
when alive. Separately, the STALE/DEAD bands (60s/110s) were
aggressive enough that brains with normal 60-90s ping cadence
oscillated LIVE → STALE → LIVE on every cycle.

### Shipped
1. **`shared/runtime/sidecar_checkin.py`** — after a successful
   sidecar check-in upsert, also bump `shared_heartbeats.last_seen`
   for the brain. Best-effort (try/except, swallows errors —
   identity-record is the canonical contract, heartbeat is a
   side-effect). Carries `detail.source = "sidecar_checkin"` so the
   operator can see WHERE the bump came from.
2. **`namespaces.py`** — heartbeat band re-tuning (visibility only,
   never affects authority/routing):
   - `HEARTBEAT_OK_BELOW_SECONDS`: 60 → 120
   - `HEARTBEAT_PREVIEW_DRIFT_SECONDS`: 110 → 300
   - `HEARTBEAT_STALE_AFTER_SECONDS`: 90 → 240
   Two full ~60-90s ping cycles fit comfortably inside `ok` before
   the badge slips to STALE.
3. **`shared/heartbeat_ping.py`** — `hb_fresh` threshold raised 90 →
   300 to keep `/heartbeat-status/{brain}` in sync with the new
   bands (otherwise its `connected/partial/stale/dead` classifier
   would disagree with the Diagnostics table).
4. **`routes/sidecar_diagnostics.py`** — `HB_FRESH_SEC` raised
   90.0 → 300.0 for the same reason.
5. **`routes/brain_emission_diagnose.py`** — `heartbeat_fresh`
   threshold raised 120 → 300.
6. **`frontend/src/pages/Diagnostics.jsx`** — DEAD-tier tooltip
   text updated from "≥110s" → "≥300s".
7. **`tests/test_drift_and_governor_exclusion.py`** — band assertions
   updated to the new 120s/300s thresholds (preserving the doctrine
   tripwire that `preview_drift` must never return).
8. **`tests/test_sidecar_checkin.py`** — new test
   `test_post_sidecar_checkin_also_bumps_heartbeat` pins the
   side-effect: POST check-in then GET `/heartbeat-status/{brain}`
   must show `heartbeat_age_seconds < 30`.

### Verified
- Curl round-trip on REDEYE: status went `connected: dead`
  (`heartbeat_age_seconds: null`, last_seen 3d ago) → `connected:
  partial` (`heartbeat_age_seconds: 0.0`, last_seen now) from a
  single sidecar check-in.
- Diagnostics page screenshot: REDEYE now shows STALE 191s
  (correctly in the new band — would have been DEAD under the old
  bands).
- 17/17 focused tests green (`test_sidecar_checkin.py` +
  `test_drift_and_governor_exclusion.py`).
- Pre-existing flake in `test_heartbeat_status::test_never_connected_state`
  reproduces on stashed (pre-change) code → confirmed unrelated to
  this pass.

### Diagnostic finding — RedEye sovereign silence
RedEye's `sovereign_state.updated_at` froze on 2026-05-31 14:16 UTC
(~3 days ago). The sidecar identity check-in path is still alive
(21 fresh check-ins/hour observed on prod), but the brain's
sovereign-tick loop has stopped writing. Suspected: brain-side
task crash inside the sidecar pod (identity-checkin daemon runs
in a separate task that survived). Recommended operator action:
call `GET /api/admin/sovereign/contribution-health?window=200` on
prod — this endpoint already exists and returns per-brain
pushed_200/rejected_422/error split + latest_outcome + top
empty_fields. If RedEye shows `health: no_data` with `latest_ts`
of 2026-05-31, the brain has stopped CALLING the endpoint entirely
(not getting rejected) — fix is brain-side (restart RedEye's pod
or its sovereign-tick task).


## 2026-02-17 (pass #58) — Sidecar imposter scan endpoint + UI tile

### Operator pin
The `sidecar_checkin_audit` collection has been collecting append-only
identity rows for every brain check-in since pass #47, but nothing
read them. This pass adds a query that surfaces ANY runtime that has
shown TWO+ distinct identities in a recent window. RedEye-side spec
calls this "the imposter signal."

### Shipped
1. **`routes/sidecar_imposter_scan.py`** — `GET /api/admin/runtime/sidecar-imposter-scan?window_hours=N`:
   - Bounded window 1-168h.
   - Per-runtime aggregate of distinct
     `(env_name, pip_freeze_sha256, source_ip, git_sha, process_identity)`
     in the audit log.
   - Flags `imposter_suspected=true` when:
     - `env_name` diverges (preview-pod claiming prod, etc.)
     - `pip_freeze_sha256` diverges (different bundle)
     - `process_identity (pid, hostname)` diverges (duplicate pods)
     - runtime is UNKNOWN (not in DISCUSSION_PARTICIPANTS)
   - `MULTIPLE_GIT_SHAS` is flagged but NOT marked imposter (legitimate
     during a deploy rollover window).
2. **`frontend/src/components/ImposterScanCard.jsx`** — Diagnostics
   page tile. Window selector (1h / 6h / 24h / 72h / 168h), per-runtime
   row with reasons and counts, top banner green/red. Read-only.

### Verified
- Endpoint returns clean shape on preview (no audit rows yet because
  preview brains aren't pinging — expected).
- 93/93 tests green across trading-path + memory layers + bus +
  autonomy + RISE AI.
- Lint clean on both new files.

### Doctrine pins preserved
- Endpoint is READ-ONLY. Flags only. Never restarts a pod or
  modifies the audit log.
- Audit log itself is still append-only (one row per POST).
- `env_name`, `pip_freeze_sha256`, `local_execution_authority`,
  `broker_mode` validations on the WRITE path remain authoritative
  (rejecting a malformed stamp at ingest is doctrine; this scan is
  the operator's view on top of those rejections).

### Net surface
- **Write path:** `POST /api/admin/runtime/sidecar-checkin/{brain}`
  validates every stamp and audits one row per POST. Doctrine pin.
- **Read path:** `GET /api/admin/runtime/sidecar-imposter-scan` flags
  any runtime with divergent identities. Operator surface.

---


## 2026-02-17 (pass #57) — Shelly Bus: brain → MC memory proposals (network)

### Operator pin
Brain Shellys = collectors + local recall. MC-Shelly = canonical
storage hub + verifier. Brains DO NOT self-certify truth — they
submit memory proposals over HTTP, MC scores trust, MC decides
canonicalization.

### Shipped
1. **`shared/shelly_bus/__init__.py`** — `ShellyMemoryProposal`
   dataclass (frozen), authority pins (PROPOSAL/REVIEW/CANONICAL).
2. **`shared/shelly_bus/mc_shelly_ingest.py`** — `POST /api/mc-shelly/memory/propose`:
   - Auth: `X-Runtime-Token` per brain (matches
     `{BRAIN}_INGEST_TOKEN` env, same scheme as every other brain
     endpoint). Cross-impersonation blocked (Camaro token can't post
     as Alpha).
   - Tampered-authority defense: brain MUST stamp
     `MEMORY_PROPOSAL_ONLY`; anything else → 400.
   - Trust scoring (env-tunable):
     - verified_outcomes match → 0.90 (VERIFIED)
     - ≥ N other brains agree   → 0.80 (CONVERGED)
     - otherwise                → 0.35 (UNVERIFIED)
   - `trust_score >= MIN_CANONICAL_TRUST` (default 0.75) →
     translates to `ShellyMemoryEvent` and ingests via
     `MCShelly.ingest_rollup` so the row joins existing canonical
     `shelly_mc_shared_memory`.
   - Below threshold → parked in `shelly_memory_proposals` for
     operator review / later auto-converge.
   - `GET /api/mc-shelly/memory/proposals/summary` — count by status.
3. **`shared/shelly_bus/brain_shelly_client.py`** — thin httpx
   `BrainShellyClient` for brain pods. Construction enforces
   `mc_url` + `runtime_token`. Has `propose_memory` (single) and
   `propose_many` (capped concurrency).

### Doctrine pins preserved
- Authority re-stamped at MC boundary (REVIEW → CANONICAL or
  PROPOSAL_ONLY); brain's claimed authority NEVER leaks through.
- Tampered authority rejected with 400 (loud, not silent rewrite).
- Trust scoring is purely advisory until it crosses the canonical
  threshold; below threshold rows are stored as proposals only.
- Endpoint mounted at `/api/mc-shelly/...` per integration spec.

### Verified
- 7/7 new bus tests green (auth, cross-impersonation, tampered
  authority, unverified pen storage, two-brain convergence
  canonicalization, summary endpoint, client construction guards).
- 97/97 broader tests green (trading + memory + autonomy + bus).
- Backend hot-reloaded; `/api/health` ok.

### Env tunables
```
SHELLY_BUS_MIN_CANONICAL_TRUST  0.75
SHELLY_BUS_VERIFIED_TRUST       0.90
SHELLY_BUS_CONVERGED_TRUST      0.80
SHELLY_BUS_UNVERIFIED_TRUST     0.35
SHELLY_BUS_MIN_CONVERGENCE      2
```

### Brain pod integration
Each brain pod:
```python
from shared.shelly_bus import ShellyMemoryProposal
from shared.shelly_bus.brain_shelly_client import BrainShellyClient

shelly = BrainShellyClient(
    mc_url=os.environ["MC_URL"],
    runtime_token=os.environ["MY_INGEST_TOKEN"],
)

await shelly.propose_memory(ShellyMemoryProposal(
    source_brain="camaro",
    lane="crypto",
    symbol="BTC/USD",
    event_type="market_pattern",
    text="BTC compression with rising volume looked similar to prior breakout setups.",
    confidence=0.67,
    regime="compression",
    outcome="pending",
    source_id=decision_id,
))
```

---


## 2026-02-17 (pass #56) — MC-Shelly L3 + L6 + Brain MEMORY.md

### Operator pin
Three of the four missing layers from the 6-layer end-goal land here.
L5 (Qdrant) parked per operator until the cluster exists.

### Shipped — `shelly/verified_facts.py` (L3 + L6)
1. **L3 Verified Fact Memory**:
   - New collection `shelly_verified_facts`.
   - `certify_one(event_hash, via, operator, note)` — operator or auto
     promotion, idempotent on event_hash.
   - `auto_certify_scan(limit)` — scans shared memory; promotes any
     event_hash that has converged across ≥ 3 brains AND has ≥ 1
     resolved outcome. Bounded by `limit`.
   - `verified_facts_summary()` — dashboard tile data.
2. **L6 RISEDUAL Wiki**:
   - New collection `risedual_wiki`.
   - `curate_wiki_run(limit)` — groups verified facts by
     `(symbol, direction)`, summarizes (win/loss/flat, avg pnl, top
     features, brains seen, avg confidence) into one wiki row per
     topic. Idempotent upsert.
   - `wiki_summary()` and `wiki_lookup(symbol, direction)`.

### Shipped — `shelly/memory_profile.py` (Brain MEMORY.md)
- `render_brain_memory_md(brain, recent_limit)` — pure-read renderer
  that pulls a LocalShelly's state and emits markdown: totals,
  win/loss/flat, top symbols, direction mix, top features, MC + RG
  status seen, recent events table.

### Shipped — `routes/shelly_admin_extension.py`
Mounted at `/api/admin/shelly/*`:
- `POST /verified-facts/certify` (operator countersign)
- `POST /verified-facts/auto-scan` (bounded auto-promotion)
- `GET  /verified-facts/summary`
- `POST /wiki/curate`
- `GET  /wiki/summary`
- `GET  /wiki/lookup?symbol=...[&direction=BUY]`
- `GET  /memory-md/{brain}?recent_limit=N` — plain text/markdown
- `GET  /memory-md?recent_limit=N` — all four brains concatenated

### Authority pin (preserved)
Every new surface stamps `authority: memory_reasoning_only`. No new
execution path. Verified-fact verdicts are PROVENANCE, not PERMISSION
— a brain still has to clear the full gate chain (seat policy etc.)
to trade.

### Verified
- 11/11 new Shelly extension tests green (live Mongo path:
  seed → certify → auto-scan → curate → wiki_lookup → MEMORY.md).
- 67/67 trading-path tests green — untouched.
- Live preview API hits return real data:
  - `verified-facts/summary`: 1 auto_convergence fact present.
  - `wiki/summary`: 1 wiki entry present.
  - `memory-md/alpha`: rendered markdown with totals + table.

### Holds
- L5 Qdrant vector recall — parked. Needs a cluster URL.

---


## 2026-02-17 (pass #55) — Auto-grader: closes the LLM training feedback loop

### Operator pin
Until something writes `grade: 1` to `llm_calls` rows, the RISE AI
training corpora stay empty. The auto-grader is the missing piece —
a rubric LLM that scores REASONING QUALITY (not market outcome) on a
two-line output, with a defensive parser that refuses to write a
silent default when the grader response is malformed.

### Shipped
1. **`shared/rise_ai/auto_grader.py`**:
   - `compose_grading_prompt(role, prompt, response)` — pure function.
   - `parse_grade(text)` — defensive regex parser; returns None on
     malformed output (row stays ungraded for retry, never silently
     writes a wrong grade).
   - `grade_one(db, call_id)` — idempotent single-row grader. Skips
     already-graded rows, the grader's own role, and empty rows
     (auto-marks empty rows as grade=0).
   - `grade_batch(db, limit=50)` — bounded batch grader. Never exceeds
     `limit` rubric LLM calls per invocation.
   - `RUBRIC` — explicit reasoning-quality rubric, pins that the
     grader is not measuring trade profitability.
   - `TRAINABLE_ROLES` — 8 canonical seats + 3 legacy aliases. The
     grader's own role (`auto_grader`) is EXCLUDED from grading
     targets (no infinite loop, never appears in training corpora).
2. **`scripts/run_auto_grader.py`** — cron-friendly operator one-shot.
   `AUTO_GRADER_LIMIT` env var controls batch size.
3. **`routes/rise_ai_admin.py`**:
   - `POST /api/admin/rise-ai/auto-grade?limit=N` — fire a grading
     batch (cost-bounded, max 500).
   - `GET /api/admin/rise-ai/grading-stats` — dashboard tile data:
     total/ungraded/g1/g0 counts + per-role ungraded breakdown.
   - Both endpoints mounted on `server.py`.
4. **`tests/test_ai_autonomy_no_execution_imports.py` upgraded**:
   - Substring-search replaced with AST-based import walk.
   - The crude string match was failing on legitimate doctrine prompts
     (e.g. "Kraken readiness" in the crypto-executor focus list,
     "RoadGuard" in the auto-grader rubric). An AST walk only flags
     ACTUAL imports.
   - Guards both `shared/ai_autonomy/` and `shared/rise_ai/`.
5. **`tests/test_auto_grader.py`** — 9 parser/filter tests covering
   happy path, case insensitivity, preamble tolerance, unparseable
   rejection, missing-reason placeholder, forbidden-role exclusion,
   and trainable-role canonical-seat coverage.

### Verified
- 27/27 RISE AI + AI-autonomy tests green (8 new auto-grader tests
  + upgraded firewall + existing surface).
- 67/67 trading-path tests green — nothing disturbed.
- Lint clean across new files.
- Live `GET /api/admin/rise-ai/grading-stats` confirmed:
  25 total / 25 ungraded / per-role: public_narrator=20, auditor=3,
  strategist=1, opponent=1.

### Operator usage
- **One-shot grade:** `POST /api/admin/rise-ai/auto-grade?limit=50`.
- **Dashboard:** `GET /api/admin/rise-ai/grading-stats`.
- **Cron:** `python -m scripts.run_auto_grader` (with optional
  `AUTO_GRADER_LIMIT` env). Safe to run on a schedule — bounded by
  `limit` so each run has a known cost ceiling.
- **After grading:** re-run `python -m scripts.rise_ai_bootstrap`.
  Datasets pick up newly-graded rows automatically.

---


## 2026-02-17 (pass #54) — RISE AI refactored seat-keyed (was brain-keyed)

### Operator pin
The `llm_calls` ledger is keyed by SEAT, not BRAIN. Filtering by brain
name returned 0 rows every time — datasets were always empty. The
8-seat IP also makes the seat the unit of authority and promotion, so
training and checkpoint identity must live on the seat. Brain rotations
no longer break training continuity.

### Shipped
1. **`shared/rise_ai/role_profiles.py`** — fully rewritten:
   - 8 canonical seat profiles (equity: strategist/auditor/governor/executor;
     crypto: crypto_strategist/crypto_auditor/crypto_governor/crypto).
   - Crypto profiles carry crypto-flavored focus (funding rate,
     liquidation cascade, stablecoin depeg, Kraken readiness, etc.).
   - Legacy alias map: `decider → strategist`, `opponent/advisor →
     auditor`, `crypto_decider → crypto_strategist`, `crypto_opponent
     → crypto_auditor`, `crypto_executor → crypto`. Resolved before
     lookup so older callers don't silently get GENERAL_PROFILE.
2. **`scripts/rise_ai_bootstrap.py`** — refactored:
   - Iterates the 8 seats (replaces `BRAINS` tuple).
   - Deprecates legacy brain-keyed checkpoints
     (`rise-ai-{alpha|camaro|chevelle|redeye}-qwen3-8b-v1` →
     `state=DEPRECATED`) on first run. Idempotent.
3. **`tests/test_rise_ai_surface.py`** — rewritten:
   - 9 tests covering all 8 seats, legacy alias resolution, lane
     isolation (crypto seats must carry crypto-flavored focus),
     model_id slugs, prompt composition.

### Verified
- 17/17 RISE AI surface tests green.
- 56/56 trading-path tests green (execution / lane / promotion /
  auto-router). Nothing disturbed.
- Live preview DB state after bootstrap:
  - 8 seat-keyed checkpoints at state=SHADOW.
  - 4 legacy brain-keyed checkpoints at state=DEPRECATED.
- Confirmed: 0/25 `llm_calls` rows are graded today → datasets
  correctly stay at 0 rows. Doctrine pin: un-graded calls aren't
  training data. Operator needs to grade (or wire an auto-grader)
  before corpora will grow.

### Next operator action to unlock training data flow
Either manual-grade a sample of `llm_calls`:
```
db.llm_calls.update_one({"call_id": "..."}, {"$set": {"grade": 1}})
```
or wire an auto-grader job that scores recent calls via a rubric LLM
and writes `grade: 1` for positives. The bootstrap script can be
re-run any time afterward — `register_checkpoint` is idempotent.

---


## 2026-02-17 (pass #53) — RISE AI role profiles + prompt composer + bootstrap

### Consolidated from operator scaffold
The scaffold proposed three new modules. After auditing the existing
codebase (`shared/llm/routing_policy.py::ROLE_OVERRIDES`,
`shared/llm/kernel.py::_default_system`) we kept two ideas and
discarded the duplicates:

- KEPT: `shared/rise_ai/role_profiles.py` — net-new per-brain
  `focus`/`forbidden`/`model_id` registry. Nothing else has this.
- DISCARDED: standalone `model_router.py` — would duplicate a single
  dict lookup. Folded `model_for_role()` into `role_profiles.py`.
- RE-SHAPED: `_compose_prompt` — extracted as the free function
  `compose_role_aligned_prompt(...)` in `shared/rise_ai/prompt_composer.py`
  so every brain pod imports the SAME canonical implementation. MC's
  own `_default_system` (kernel.py) stays untouched — it's the
  fallback for external Anthropic/OpenAI/Gemini calls.

### Shipped — `shared/rise_ai/`
1. **`role_profiles.py`**:
   - `RISE_AI_ROLE_PROFILES` dict for alpha/camaro/chevelle/redeye.
   - `GENERAL_PROFILE` fallback (no crashes on typo brain names).
   - `profile_for(role)` graceful-fallback lookup.
   - `model_for_role(role)` checkpoint id helper.
2. **`prompt_composer.py::compose_role_aligned_prompt`** — single
   source of truth for the role-aligned brain-prompt scaffold. Pins
   `authority: REASONING_ONLY` in every output, embeds the role's
   focus + forbidden lists, accepts optional memory/market/doctrine
   contexts.
3. **`__init__.py`** — public surface re-export.

### Shipped — `scripts/rise_ai_bootstrap.py`
One-shot operator action that for each brain:
1. Builds `/app/backend/datasets/rise_ai/{brain}.jsonl` from
   graded `llm_calls` rows (`build_training_jsonl`).
2. Registers a SHADOW-state checkpoint with the canonical model_id
   (`register_checkpoint`) — idempotent by `model_id`.

Path bug from operator scaffold fixed (`from app.backend.db import db`
→ `from db import db`).

### Verified
- 15/15 RISE AI surface tests green.
- 67/67 trading-path tests green (execution gates / lane toggles /
  promotion gate / auto-router / seat policy) — nothing disturbed.
- Bootstrap script ran end-to-end against live preview DB:
  4 checkpoints registered at SHADOW, idempotent re-run skips
  existing rows. 0 dataset rows expected on preview (no llm_calls
  history); prod will accumulate as brains run.
- Lint clean across new files.

### What this unlocks
Each brain now has a canonical checkpoint identity
(`rise-ai-{brain}-qwen3-8b-v1`) in `ai_checkpoints` at SHADOW state.
External trainer pulls the per-brain dataset, fine-tunes, and the
result lands at the corresponding model_id. Operator transitions
SHADOW → ADVISOR → PRIMARY via `set_checkpoint_state` based on
`evaluate_candidate_model` recommendations. Routing already honors
this (priority walk in `routing_policy.py`).

---


## 2026-02-17 (pass #52) — AI autonomy pipeline (advisory side-band)

### Operator pin
A read-only side-band of the LLM stack. Trains and grades local/self-
trained candidate models against the commercial primary, but the
authority firewall (`test_ai_autonomy_no_execution_imports.py`) refuses
any import of execution, RoadGuard, or a broker adapter. The whole
package is ADVISORY_ONLY by construction.

### Shipped — `shared/ai_autonomy/`
1. **`promotion_gate.py`** — pure eval-math, no I/O.
   - `EvalResult` dataclass.
   - `can_promote_to_advisor` (eval≥100, agreement≥0.80, safety==0, hallucination≤0.05).
   - `can_promote_to_primary` (eval≥500, agreement≥0.85, win_rate≥0.52, safety==0, hallucination≤0.03).
   - `PromotionState` enum: OFFLINE / SHADOW / ADVISOR / PRIMARY / ROLLBACK.
2. **`dataset_builder.py::build_training_jsonl`** — reads `llm_calls`
   filtered by role + grade≥min_grade, writes JSONL training corpus.
   Excludes `_id` (BSON pin), UTC-aware timestamps.
3. **`shadow_compare.py::shadow_compare`** — runs primary (`anthropic`)
   AND candidate (`local`) for the same prompt via
   `llm_kernel.call(provider_override=...)`. Both responses logged in
   `llm_calls` with `comparison_lane` metadata.
4. **`checkpoint_registry.py`** — `register_checkpoint`,
   `set_checkpoint_state` over `ai_checkpoints`. UTC-aware. Insert
   never returns the mutated input dict.
5. **`autonomy_loop.py::evaluate_candidate_model`** — aggregates
   `llm_eval_runs` for (role, model_id) and writes a
   `KEEP_SHADOW / PROMOTE_TO_ADVISOR / PROMOTE_TO_PRIMARY` recommendation
   row to `ai_promotion_recommendations`. Never transitions state.
6. **`__init__.py`** — public surface re-export.

### Authority firewall test
- `tests/test_ai_autonomy_no_execution_imports.py` — scans every `*.py`
  under `shared/ai_autonomy/` and FAILS if it contains any of:
  `shared.execution`, `shared.broker_router`, `roadguard`, `alpaca`,
  `kraken`, `submit_order`, `place_order`. Case-insensitive.

### Promotion-gate tests
- 6 truth-table tests covering both rungs, safety zero-tolerance,
  sample-size floor, coin-flip rejection, and the advisor-vs-primary
  hallucination delta. **7/7 tests passing.**

### Routing already wired
The "local/self_trained PRIMARY beats anthropic" semantics requested
in the scaffold's bash block are already implemented by
`shared/llm/routing_policy.py::choose_model` — it walks `PROVIDER_PRIORITY`
(local first, then self_trained, then external) and picks the first
provider that is both `is_ready()` AND promoted to ADVISOR/PRIMARY. The
operator transitions the promotion via `set_checkpoint_state` → which
the kernel reads through `llm_provider_state`. No router change needed.

### Verified
- All ai_autonomy modules lint clean.
- `import shared.ai_autonomy` succeeds at backend boot.
- Backend `/api/health` ok.

---


## 2026-02-17 (pass #51) — Aggressive auto-router config + lane toggle is now master kill

### Operator pin
"Let it rip" config. Auto-router fires brain BUY/SELL intents through the
broker without operator approval. Lane toggle in the UI is the master kill
switch; everything else stays suspended.

### Shipped
1. **`namespaces.py::SEAT_LAYER_GATES`** — added `lane_execution_enabled`.
   This gate is now AUTHORITATIVE again (no longer in the suspension list).
   With every other patent suspended, the lane toggle is the operator's
   only "stop trading NOW" surface; it must block.
2. **`shared/auto_router.py`**:
   - `AUTO_ROUTER_NOTIONAL_USD` default: $100 → **$10**.
   - `RISEDUAL_EXEC_CONFIDENCE_FLOOR` default: 0.35 → **0.30**.
   Both still env-overridable. Production picks up the new defaults
   without an .env change.

### Net behaviour
- A brain emits `action=BUY action=SELL` with `confidence ≥ 0.30` → the
  next 30-second auto-router tick picks it up.
- Gate chain runs: seat check + lane toggle + action_routable +
  schema_invariants. Every other patent gate runs advisory-only (still
  surfaces what it would have blocked, but force-passes).
- If seat is held and lane is enabled, intent goes to the broker for
  $10. No operator click.
- Operator hits the lane toggle in UI → `lane_execution_enabled` blocks
  → auto-router skips that lane until re-enabled.

### Tests
- `test_lane_execution_toggles::test_gate_chain_blocks_when_lane_execution_off`
  reverted to asserting the gate BLOCKS (no longer suspended).
- `test_execution_gates._patches` extended with `lane_enabled` flag
  (default True) so happy-path tests don't have to seed
  `shared_lane_execution_toggles`.
- 56/56 tests green across `test_execution_gates`,
  `test_lane_execution_toggles`, `test_auto_router_helpers`,
  `test_promotion_gate`.

### Operator quick-ref
- Stop ALL trading instantly: turn off BOTH lane toggles in the UI.
- Resume: turn them back on; next 30s tick re-engages.
- Change order size globally: set `AUTO_ROUTER_NOTIONAL_USD` env var.
- Per-intent override: brain can stamp `requested_notional_usd` on the
  intent and the auto-router will use that (clamped by sizing gate).

---


## 2026-02-17 (pass #50) — Operator-Inject button + opponent label cleanup

### Shipped
1. **`frontend/src/components/OperatorInjectIntent.jsx`** — new admin
   modal trigger in the Intents page header (`ENTER MISSION CONTROL` →
   Intents → top-right `OPERATOR INJECT` pill). Replaces the
   browser-console workaround. Workflow:
     - Pick lane (crypto/equity)
     - Auto-resolves the current executor-seat holder from
       `/admin/seat-registry/diagnose` (RedEye for crypto, Alpha for
       equity in your prod state).
     - Pick symbol (with preset chips: ETH/BTC/SOL/LINK or SPY/QQQ/...).
     - Pick BUY/SELL.
     - Type the notional `$`.
     - Type confirm phrase `operator dip-buy` to enable fire.
     - One button fires both `POST /intents` (under the seat holder) and
       `POST /execution/submit` (with operator notional).
     - Result panel shows order status, broker_id, txid, fill price OR
       the failing gate with per-gate reasons.
2. **`pages/Intents.jsx`** — header wired with `<OperatorInjectIntent />`.
3. **`pages/Login.jsx`** — REDEYE label `OPPONENT` → `AUDITOR`.
4. **`pages/Overview.jsx`** — seat enumeration list updated to the
   canonical 4 seats (EXECUTOR, STRATEGIST, GOVERNOR, AUDITOR). Removed
   the obsolete DECIDER/ADVISOR/OPPONENT bullets.

### Verified
- Lint clean across all 4 touched files.
- Component renders a pill button when closed, modal when opened, with
  confirm-phrase guard preventing accidental fire.

### Not touched (intentional, separate concern)
- Marketing landing page (`risedual/pages/Landing.jsx`) still uses the
  word "Opponent" — that's a public-facing positioning doc, separate
  from MC operator surface.
- `RosterPanel.jsx` / `ParadoxRosterPanel.jsx` still reference the
  `opponent` schema key (intentional — backend schema key is still
  `adversary`/`opponent` for audit continuity; only user-facing labels
  flip to "Auditor"). Will need a follow-up sweep if you want the
  Paradox roster panel's UI text changed too.

---


## 2026-02-17 (pass #49) — PATENT SUSPENSION: only seat policy gates execution

### Operator pin
Cascading post-crash, the Patent-stack restrictions (J readiness, broker
lanes, RoadGuard, R:R, exposure caps, council verdict, confidence floors)
were locking every brain out of execution. Operator directive: suspend
every non-seat restriction. Seat policy remains the sole authoritative gate.

### Shipped
1. **`namespaces.py`**:
   - New `PATENT_SUSPENSION_ACTIVE = True` master flag.
   - New `SEAT_LAYER_GATES` frozenset — the gates that stay authoritative:
     `schema_invariants`, `action_routable`, `executor_seat_check`,
     `live_trading_disabled`. Everything else is force-passed while the
     master flag is on.
2. **`shared/execution.py::_evaluate_gates`** — after every gate runs,
   any gate not in `SEAT_LAYER_GATES` with `passed=False` is mutated to:
   - `passed: True`
   - `suspended: True`
   - `doctrine_reason: <original reason>`
   - `reason: "[SUSPENDED — Patent-stack restrictions lifted by operator] <original>"`
   The verdict is then computed off the rewritten chain. Audit trail of
   what doctrine WOULD have said is preserved on every gate row.
3. **`shared/promotion.py::evaluate_readiness`** — final `passed` is forced
   True under suspension. The 8 checks still run and surface in the
   response; new `suspended: bool` + `doctrine_passed: bool` fields expose
   what Patent J would have said.
4. **Tests updated**:
   - `test_gate_chain_blocks_when_broker_disconnected` → now asserts
     `passed=True, suspended=True, doctrine_reason preserved`.
   - `test_gate_chain_blocks_when_daily_cap_would_be_breached` → same
     suspension contract.
   - `test_gate_chain_blocks_when_lane_execution_off` → same.

### Verified
- 96/96 tests green across execution/promotion/lane/seat/rr/roster suites.
- Live `_evaluate_gates` run on a crypto BUY intent shows expected pattern:
  only `executor_seat_check` blocks (seat vacant in preview), every other
  non-seat gate force-passes with `[SUSPENDED]` tag + preserved
  doctrine_reason.
- Lint clean.

### Reverting
Set `namespaces.PATENT_SUSPENSION_ACTIVE = False` and redeploy. Every gate
becomes authoritative again. No code changes required — the audit trail
of `suspended:true` rows lets the operator see what was let through
during the suspension window.

### Doctrine pin (preserved through suspension)
- Seat policy (who can execute what lane) — STILL AUTHORITATIVE.
- Schema invariants (`may_execute=False`, `requires_gate_pass=True`) —
  STILL AUTHORITATIVE (value-shape pin, not a restriction).
- `action_routable` — STILL AUTHORITATIVE (HOLDs literally aren't orders).

---


## 2026-02-17 (pass #48) — Seat registry drift banner on Intents page

### Shipped
1. **`frontend/src/components/SeatRegistryDriftBanner.jsx`** — read-only,
   polls `/admin/seat-registry/diagnose` every 30s. Renders nothing when
   the registry is healthy (no drift + every lane has a holder). Renders
   a red banner at the top of the Intents page when:
   - The roster and the legacy `shared_executor_seat` doc disagree
     (per `diagnose.drift`), OR
   - Any lane reports `would_route_pass: false` (vacant executor seat).
2. **`pages/Intents.jsx`** — mounted the banner directly under the page
   header so it's the first thing visible.

### Verified
- Lint clean on `SeatRegistryDriftBanner.jsx` + `pages/Intents.jsx`.
- Frontend + backend supervisor running.
- Banner copy includes the canonical fix path: "Source of truth: Quick
  Seat Switches. Click a brain pill on the Seats panel to assign."

### What it solves
The operator no longer needs to know `/admin/seat-registry/diagnose`
exists. The page screams when the registry is split-brain or a lane
is unstaffed. Catches drift in ≤30s instead of accumulating days of
executor_seat_check blocks.

---


## 2026-02-17 (pass #47) — Seat registry precedence flip + diagnostic endpoint

### Operator pin
The execution gate was silently reading from a stale `shared_executor_seat`
doc (last touched 2026-05-19) instead of the live roster updated via the
Quick Seat Switches UI. That made `executor_seat_check` block intents
under a holder the operator no longer thought was in the seat.

### Shipped
1. **`shared/executor_seat.py::get_seat_holder` — precedence flipped**:
   - Reads multi-seat roster FIRST.
   - Falls back to legacy `shared_executor_seat` doc ONLY if the roster
     has no assignment for `executor`.
   - Other seats (governor, auditor, crypto*) were already roster-only.
   - QSS UI is now the unambiguous source of truth.
2. **New `GET /api/admin/seat-registry/diagnose`** (read-only):
   - Returns roster assignments + legacy doc + per-seat gate view + drift
     detection + per-lane "would_route_pass" summary.
   - One JSON answers "is the gate seeing the right holder?" without
     reading code or two collections.
   - Mounted at `routes/seat_registry_diagnose.py`.

### Verified
- Preview gate now sees `alpha` for executor (roster), no longer
  `camaro` (legacy doc from 2026-05-19).
- 142/143 tests pass across roster/seat/promotion/lane/doctrine suites.
  The 1 failure (`test_gate_chain_passes_when_everything_aligned`) is
  pre-existing lane-toggle fixture drift, unrelated to this change.
- Lint clean on touched files.

### Known issue surfaced by diagnose (NOT a bug — operator action)
- Crypto executor seat (`crypto`) is vacant in the preview roster.
  Quick Seat Switches has no assignment. Diagnostic correctly reports
  `would_route_pass: false` for crypto lane. Operator must assign on
  prod's QSS panel before any crypto intent will route.

---


## 2026-02-17 (pass #46) — Doctrine packet purged of legacy seat names; UI surfaces real block reason; operator-typed notional

### Operator pin
The doctrine sidecar UI was showing `crypto_decider` and `crypto_opponent` as seat names with `holder: vacant`. Those seats were renamed in the 2026-05-31 8-seat IP refresh; the roster has been storing canonical names (`crypto_strategist`, `crypto_auditor`) but the doctrine packet builders + `fetch_seat_holders` were still asking for the legacy keys → permanent "vacant" labels. Fixed.

### Shipped
1. **`shared/doctrine/lane_doctrine_router.py::fetch_seat_holders`**: now reads canonical roster keys.
   - Equity: `strategist`, `auditor`, `governor`, `executor` (was `decider`, `opponent`, ...).
   - Crypto: `crypto_strategist`, `crypto_auditor`, `crypto_governor`, `crypto` (was `crypto_decider`, `crypto_opponent`, ...).
2. **`shared/crypto/doctrine/crypto_brain_sidecars.py::CRYPTO_SEAT_MAP`**:
   - `strategist → crypto_strategist`, `adversary → crypto_auditor` (was `crypto_decider`, `crypto_opponent`).
3. **`shared/doctrine/brain_sidecars.py::EQUITY_SEAT_MAP`**:
   - `strategist → strategist`, `adversary → auditor` (was `decider`, `opponent`).
   - Holder lookups in `build_all_brain_doctrine_packets` updated to read canonical keys.
4. **Frontend `pages/Intents.jsx::runSubmit`** (preview only, prod redeploy required):
   - Operator-typed notional prompt before broker route. Defaults to lane per-order cap, refuses any value > cap.
5. **Frontend `pages/Intents.jsx` submit-error render** (preview only, prod redeploy required):
   - Renders `blocked_by` + `reason` + per-failing-gate list when `/execution/submit` 403s. Replaces the previous bare "HTTP 403" string.

### Verified
- 112/112 tests green across `test_doctrine_sidecars`, `test_crypto_doctrine_sidecar`, `test_promotion_gate`, `test_single_sign_promotion`, `test_dual_sign_promotion`, `test_roster`, `test_seat_policy_and_auto`, `test_opponent_auditor_merge`.
- Backend hot-reloaded, `/api/health` ok.
- Updated tests `test_crypto_packet_records_seat_holders`, `test_brain_can_hold_seats_in_both_lanes_simultaneously`, `test_each_seat_has_seat_and_holder_fields`, `test_packet_records_holder_when_provided` to assert canonical seat names.

### Known carryover (NOT touched this pass)
- `test_doctrine_intent_attachment::test_equity_with_empty_snapshot_still_returns_packet` still asserts the pre-doctrine-c `governor_action == "block"`. Chevelle no longer hard-blocks (modulate-only under doctrine-c). Test is in the legacy drift backlog per operator directive (delete-obsolete-shape).

---


## 2026-02-17 (pass #45) — Cosigner removed; /propose auto-elevates on Patent J pass

### Operator pin
Solo-operator deployment. The `/admin/promotion/propose` call already requires an
authenticated admin JWT — that's the human sign. A second click on `/countersign`
by the same human added zero safety. Removed.

### Shipped
1. **`namespaces.py`**: new `REQUIRE_COUNTERSIGN = False` toggle. Flip to `True`
   when helpers are added; no other code change needed.
2. **`shared/promotion.py::propose_from_latest_artifact`**:
   - When `readiness.passed and not REQUIRE_COUNTERSIGN`, the propose call now
     **immediately elevates** authority state and writes a single-signer audit
     entry with `via="operator_propose_auto_elevate"`.
   - Returns shape extended with `auto_elevated: bool`, `from_state`, `to_state`.
   - When readiness fails: behaviour unchanged — proposal stays `pending`, no
     elevation, operator re-proposes after fixing the gate.
3. **`/countersign` and `/reject` endpoints**: untouched. `/countersign` remains
   a legacy ratify path (still 412s on a failed readiness gate — doctrine pin).
4. **Audit history**: `shared_authority_state.history` entry tagged
   `via="operator_propose_auto_elevate"` instead of `"operator_countersign"`,
   so the audit trail visibly distinguishes auto-elevated from manually-ratified.

### Verified
- All 25 promotion tests still green (`test_promotion_gate`,
  `test_single_sign_promotion`, `test_dual_sign_promotion`).
- Backend restarted cleanly; `/api/health` ok.

---


## 2026-02-17 (pass #44) — Patent J bootstrap thresholds + stale comment cleanup

### Shipped
1. **`namespaces.py` `PROMOTION_THRESHOLDS` lowered to bootstrap-friendly values**:
   - `min_resolved_rows`: 100 → **25** (biggest unblock — early fleet can clear the sample-size floor)
   - `ece_max`: 0.05 → **0.10**
   - `brier_max`: 0.20 → **0.30**
   - `min_disagreement_stability`: 0.7 → **0.55**
   - Doctrine pins **untouched**: `max_role_violations_24h` = 0, `max_toxic_memory_24h` = 5, `heartbeat_max_age_seconds` = 300.
   - Patent J remains PASS-only; gate alone never promotes — operator countersign still required.
2. **`tests/test_public_rate_limit.py`**: removed stale autouse-fixture comment referencing the deleted `test_public_phase2` module.

### Verified
- All 25 promotion-gate tests green (`test_promotion_gate`, `test_single_sign_promotion`, `test_dual_sign_promotion`).
- Backend `/api/health` ok; supervisor backend/frontend running.
- `namespaces.py` lint clean.

---


## 2026-05-31 (pass #43) — Canonical 8-seat IP doctrine enforced; stubs cleaned

### Operator pins
- **The IP defines exactly 8 seats**, no more, no less:
    - Equity: `strategist`, `executor`, `governor`, `auditor`
    - Crypto: `crypto_strategist`, `crypto` (= `crypto_executor`), `crypto_governor`, `crypto_auditor`
- **Brains may hold ONE equity seat AND ONE crypto seat simultaneously.**
- **Governor seats (equity + crypto) restricted to Chevelle and RedEye**; every other seat is open to every brain (including Chevelle/RedEye).
- Anything outside these 8 seats is **NOT part of the IP** and must alias back in via `SEAT_ALIASES`.

### Shipped
1. **`shared/seat_policy.py` refactored**:
   - SEAT_POLICY reduced from 9 entries to the canonical 8.
   - Removed deprecated stub rows: `decider`, `advisor`, `opponent` (and their crypto twins). They live ONLY as aliases now.
   - Added missing canonical rows: `crypto_strategist`, `crypto_governor`.
   - Added new alias `crypto_executor` → `crypto` for symmetric naming.
   - Added `CANONICAL_SEATS` constant with assertion (`len == 8`, equals SEAT_POLICY keys) — guards against schema drift.
   - `snapshot()` simplified: direct lookup through canonical 8 + alias normalization; no more "crypto_* falls through to equity twin" magic.
   - `seat_may_execute_lane()` simplified: direct lookup against the 8-seat table.
   - `SEATS` export now equals `CANONICAL_SEATS`.
2. **`shared/roster.py` refactored**:
   - `ROLES` tuple reduced to the canonical 8.
   - `DEFAULT_ASSIGNMENTS` cleaned: equity defaults preserved (camaro=strategist, alpha=executor, chevelle=governor), all crypto seats and equity auditor start vacant.
3. **`shared/positions.py` fix**: `_stance_summary()` was reporting `adversarial_blindness = "opponent" in missing`, but `opponent` is no longer in `required_seats()` — flag would always be False. Updated to check for `auditor` per the 2026-05-27 doctrine merge.
4. **Test cleanup**:
   - Deleted obsolete `TestQuorum` class from `test_quorum_and_provenance.py` per operator decision ("scaffolding hiccup, never part of IP"). Provenance tests preserved.
   - Updated `test_seat_aliases.py`, `test_seat_policy_and_auto.py`, `test_opponent_auditor_merge.py`, `test_roster.py` to assert the canonical 8.

### Verified
- **67/67 tests pass** in `test_quorum_and_provenance.py + test_seat_aliases.py + test_opponent_auditor_merge.py + test_seat_policy_and_auto.py + test_roster.py`.
- Backend boots clean; 13 authority probes all pass:
    - `seat_may_execute_lane('executor','equity')` = True
    - `seat_may_execute_lane('crypto','crypto')` = True
    - `seat_may_execute_lane('crypto_executor','crypto')` = True (via alias)
    - Cross-lane: all return False
    - Legacy `decider`/`opponent` still alias correctly.

### Doctrine boundary
- The `CANONICAL_SEATS` constant + assertion is the IP boundary in code. Any commit that mutates SEAT_POLICY drift-asserts at module import — system won't boot if drift is introduced.


## 2026-05-31 (pass #42) — Prod mobile login fix: same-origin API resolver

### Operator report
"It's blocked logins again" — screenshot showed mobile Chrome at `mission.risedual.ai/login` displaying "Something went wrong. Please try again." despite valid credentials.

### Diagnosis
- Backend at `mission.risedual.ai/api/*` returns 200 with valid tokens.
- Desktop Playwright run logged in successfully and reached dashboard.
- Root cause: **prod frontend bundle was built with `REACT_APP_BACKEND_URL=https://multi-brain-backbone.emergent.host`**, while the frontend is served from `mission.risedual.ai`. That's a cross-origin call; CORS is configured correctly, but **mobile Chrome silently fails the fetch under certain third-party cookie / cross-site request modes**, producing a `null` response that surfaces as the generic "Something went wrong" string.
- Cloudflare on `mission.risedual.ai` already proxies `/api/*` to the same backend — so same-origin calls work fine and eliminate the entire cross-site surface.

### Shipped
1. **`frontend/src/lib/api.js`** — added `resolveBackendUrl()` that prefers SAME-ORIGIN when the frontend is hosted on a known prod domain (`mission.risedual.ai`, `www.risedual.ai`, `risedual.ai`). Falls back to `REACT_APP_BACKEND_URL` env on preview / dev where same-origin proxy isn't wired.
2. **Exported `BACKEND_URL`** from `lib/api.js` and converted all 6 direct `process.env.REACT_APP_BACKEND_URL` usages across the frontend to import the resolved value:
   - `risedual/lib/mc.js`, `risedual/pages/Markets.jsx`, `risedual/components/{NewsTicker,DarkPoolWidget,CandleChart}.jsx`, `pages/{Ping,McShelly}.jsx`.
3. Verified preview login still works (Playwright run: 200 OK, redirect to dashboard).

### Operator action required to roll out
Redeploy the prod frontend. **Once deployed, mobile login will hit `mission.risedual.ai/api/auth/login` (same-origin), eliminating the cross-site failure mode.**

### Doctrine pin
- `lib/api.js::resolveBackendUrl()` ALL config decisions happen at runtime, not build-time. Preview behavior unchanged.


## 2026-05-31 (pass #41) — Finnhub LIVE + 10yr historical backfill

### Operator action
Provided Finnhub API key with basic-tier access (60 rpm, 10 years of `/stock/candle` history). Earlier "access denied" was misread on my part — the key works.

### Shipped
1. **`FINNHUB_API_KEY` + `FINNHUB_ENABLED=true`** in `backend/.env`. Live poller now ticks every 5 min, fetches 5m bars for `patterns_universe` symbols (currently 8 seed symbols; should be expanded to S&P 500 — see backlog).
2. **`backend/routes/finnhub_backfill.py`** — operator-only historical backfill endpoints:
   - `POST /api/admin/feeders/finnhub/backfill/symbol` — single-symbol (blocking, ~1s for daily/10yr)
   - `POST /api/admin/feeders/finnhub/backfill/universe` — full S&P-500 (background job, rpm-throttled)
   - `GET /api/admin/feeders/finnhub/backfill/universe/{job_id}` — progress poll
   - `POST .../cancel` — cancel a running job
3. **Bulk-write persistence** — single `bulk_write` per symbol instead of per-bar `update_one`. ~5x faster, doesn't block the event loop. Other endpoints (auth, snapshots) stay responsive (380ms auth during backfill).
4. **Rate-limit fingerprinted**: Finnhub returned `x-ratelimit-limit: 60` headers — confirmed basic-tier ceiling. Default backfill rpm bumped to 50 (was 30), leaves 10/min headroom for live worker.
5. **+5 backfill tests** (happy path, no_data, fetch_failed, bad-resolution, idempotency). All pass.
6. **Brain doc + test_credentials.md updated** with key + backfill endpoints.

### Verified end-to-end
- Single-symbol NVDA daily 10yr backfill: **2,511 candles, 2016-06-03 ($1.16) → 2026-05-29 ($211.15)**, 1.2s wall time.
- **Full S&P-500 universe backfill: COMPLETE — 1,234,440 daily bars across 502 symbols, 0 failures, ~10 min wall time at 55 rpm.**
- Auth latency during backfill: **378ms** (was timing out before bulk-write fix).
- Sample range confirmed: NVDA oldest 2016-06-03 (close=$1.162), newest 2026-05-29 (close=$211.15).

### Doctrine pin
- Backfill writes to `shared_ohlcv_bars` with `source: "finnhub_equity"`, `ingested_via: "finnhub_backfill"` (distinguishable from live-polled bars). No execution authority anywhere in this path.


## 2026-05-31 (pass #40) — Polygon (Massive) daily equity feeder live

### Operator ask
"Build the Polygon feeder so daily snapshots actually populate." Public.com may return ~June 4 (information-only) — Polygon fills the gap now and stays as a redundant data source after.

### Shipped
1. **`backend/shared/feeders/polygon_equity.py`** — async worker mirroring the Finnhub feeder pattern, pulls **grouped-daily aggregates** (entire US equity market in one HTTP call, ~12k rows). Writes to `shared_ohlcv_bars` with `source: "polygon"`, `tf: "1d"`. Idempotent on `(source, symbol, tf, ts)`. Honors NYSE calendar — no pulls on weekends/holidays; waits 30 min after close (configurable) so Polygon's grouped data has finalized.
2. **`server.py` lifespan wiring** — Polygon worker boots alongside Finnhub. Boots into `already_pulled` shortcut if a recent day's bars exist (skip threshold = 5k rows).
3. **Per-tf source split in `capture_snapshot`** — separate `intraday_source` (default `finnhub_equity`) and `daily_source` (default `polygon`) so each timeframe block picks its own preferred feeder. Env vars: `MC_SNAPSHOT_INTRADAY_SOURCE`, `MC_SNAPSHOT_DAILY_SOURCE`. Captured-log audit + per-row docs both record the resolved source per timeframe.
4. **+10 new Polygon feeder tests** (shape conversion, schedule logic, idempotent pull, fetch-failure handling) and **+1 new snapshot test** (`test_capture_uses_per_tf_sources` proving the split actually picks the right feeder per block).
5. **Brain doc** updated with the per-tf source pin.

### Verified
- **26/26 tests pass** across `test_polygon_equity_feeder.py` (10) and `test_daily_market_snapshots.py` (16).
- Backend rebooted clean; first Polygon tick pulled **8,870 daily bars** for 2026-05-29 in ~1.5s.
- After capture: **484 of 502 S&P-500 daily blocks** populated with real Polygon OHLCV (NVDA: O=214.575, H=217.86, L=211.13, C=211.14, V=289.4M). 18 misses = symbols Polygon didn't have grouped-daily rows for that day (delisted/fresh, audited as `no_bars_for_symbol`).
- `bar_source` per block correctly echoes `polygon` for daily, `finnhub_equity` for intraday.

### Doctrine pin
- **Evidence only.** The Polygon feeder writes bars into MC's federation; it never carries execution authority. No `may_execute` in any ingest path.
- **Information-only on Public.com when it returns.** Per operator (2026-05-31), Public will be wired as a data feeder, not a broker. The Public daily feeder will be additive — same pattern, `source: "public"`, can run alongside Polygon for redundant coverage of equity + new crypto coverage.


## 2026-05-31 (pass #39) — Daily Snapshots: dual-timeframe OHLCV (5m + 1d)

### Operator follow-up
"Will this include OHLCV as well?" → "Yes, but you only had 1d — I want both intraday + daily" (Option C).

### Shipped
- **Schema change**: each `daily_market_snapshots` row now contains two nested blocks — `intraday` (default `tf=5m`) and `daily` (default `tf=1d`). Each block has its own `price`, `ohlc`, `asof`, `bar_source`, `price_ok`, `price_reason`, `relative_volume`, `basis_bars`, `current_v`, `avg_v`. Coverage gaps are per-timeframe (intraday may populate while daily is null).
- **Capture path**: `_build_one` now fans out 4 parallel DB queries per symbol (latest_5m, latest_1d, RVOL_5m, RVOL_1d) via `asyncio.gather`; concurrency cap of 25 keeps Motor pool healthy.
- **Capture-log audit**: summary row now reports both `intraday_rows_with_price` and `daily_rows_with_price`.
- **Fixed timeframe constants**: corrected to match the Finnhub feeder's actual writes (`5m` / `1d`, not the wrong `1Day` I had in pass #38). Env overrides: `MC_SNAPSHOT_INTRADAY_TF`, `MC_SNAPSHOT_DAILY_TF`.
- **Brain doc**: `BRAIN_API_QUICKSTART.md` updated with the new dual-block response shape + per-timeframe coverage doctrine.
- **+1 new test**: `test_capture_handles_asymmetric_coverage` proves intraday-present-daily-missing produces correct nulls in only the daily block.

### Verified
- **15/15 tests pass** (was 14, +1 for asymmetric coverage).
- Backend reboots clean; worker logs the new tf set.
- 502-symbol × 4-query operator capture completes in **~1.5s** on preview.
- Curl E2E: row payload shows both `intraday` and `daily` nested blocks per symbol.


## 2026-05-31 (pass #38) — Daily Market Snapshots (S&P-500-wide, 3x/day)

### Operator ask
"Set up snapshots throughout the day — one at open, one ~3h later, one at close — and hold them for the brains to retrieve, then wipe at the start of the next market day. Use S&P 500 because brains need to learn options trading."

### Shipped

1. **S&P-500 universe** (`shared/snapshots/sp500_universe.py`): 502 tickers pinned as a static list (alphabetical, deduped). Updated by PR when the index reshuffles — deterministic across pod restarts.

2. **NYSE calendar helper** (`shared/snapshots/nyse_calendar.py`): pure date-math (no external API). Pinned holidays for 2025/2026/2027. Exposes `is_trading_day()`, `previous_n_trading_days(anchor, n)`, `market_day_today()`, `now_eastern()`.

3. **Capture service** (`shared/snapshots/service.py`):
   - `capture_snapshot(label)` — async fan-out across the universe (concurrency=25 via semaphore), each symbol reads its latest `shared_ohlcv_bars` row (preferring `finnhub_equity` source, falling back to any source) + computes RVOL from the existing `feature_service`. Persists one upserted doc per `(market_day, label, symbol)` into `daily_market_snapshots`. Writes one audit row into `daily_snapshot_capture_log`. Idempotent.
   - `wipe_old_snapshots(keep_trading_days=5)` — deletes rows older than the Nth-most-recent NYSE trading day.
   - `ensure_indexes()` — unique compound index `(market_day, label, symbol)` + 2 query indexes.
   - 502-symbol sweep completes in **~685ms** on preview.

4. **Capture worker** (`shared/snapshots/worker.py`): single asyncio task, 30s tick cadence, 60s trigger window. Fires `open` at 09:35 ET, `midday` at 12:30 ET, `close` at 16:05 ET on NYSE trading days only. Skips weekends + pinned holidays. On the `open` capture each day, runs `wipe_old_snapshots()` first so retention is enforced lazily (no separate midnight job). Idempotent on hot reload + crash. Disable via `MC_SNAPSHOT_WORKER_ENABLED=false`. Wired into `server.py` lifespan.

5. **Retrieval API** (`routes/daily_snapshots.py`) — dual auth (operator JWT OR brain `X-Runtime-Token`):
   - `GET /api/admin/market-data/daily-snapshots/labels` — which labels captured today.
   - `GET /api/admin/market-data/daily-snapshots?label=open` — full universe for one label (filterable by `symbols=`).
   - `GET /api/admin/market-data/daily-snapshots/symbol/{symbol}` — all 3 labels today for one symbol, pivoted.
   - `GET /api/admin/market-data/daily-snapshots/history/{symbol}?days=5` — last N market days for one symbol.
   - `POST /api/admin/market-data/daily-snapshots/capture?label=open` — operator-only manual fire (e.g., backfill a missed scheduled trigger after a pod restart).

6. **14 new tests** in `tests/test_daily_market_snapshots.py`:
   - NYSE calendar (weekends, holidays, previous-N math).
   - Capture: missing-bars case, with-bars case, idempotency, bad-label rejection.
   - Wipe: keeps last 5 trading days, deletes older.
   - Worker: due-label window matching, weekend skip, already-captured idempotency.
   - SP500 universe: ≥500 unique uppercase symbols, no whitespace.

7. **Brain Quickstart doc** (`memory/BRAIN_API_QUICKSTART.md`) extended with the new section + endpoints.

### Verified
- All 14 tests pass.
- Backend reboots cleanly; worker logs `daily_snapshot worker started`.
- End-to-end curl on preview: labels, batch, single-symbol, history, bad-label rejection, bad-auth rejection — all correct.
- Operator `POST /capture` sweeps 502 symbols in 685 ms; produces audit row + 502 snapshot rows (all `price: null, price_reason: "no_bars_for_symbol"` on preview because this DB has no `finnhub_equity` bars — correct per the contract).

### Doctrine pin
- **Derived evidence only.** Capture path never hits broker quotes; reads `shared_ohlcv_bars` exclusively. Brains retrieve; no execution authority on this surface.
- **Coverage gaps are auditable.** Missing bars → `price: null, price_reason: "no_bars_for_symbol"` (never silently dropped).


## 2026-05-31 (pass #37) — Auto-router position-model alignment (the actual "no line to execute" fix)

### Operator finding
After multiple passes targeting upstream surfaces (gate chain, quorum, etc.), the operator surfaced the still-broken path: intents passing MC's gates were not reaching the broker. The line existed in code (`broker_router.route_order`, `auto_router._tick`) but a specific GC + pickup filter combo was severing it for cross-brain intents.

### Root cause (code receipts, not memory)
Two surfaces in `shared/auto_router.py` were still brain-coupled even after the 2026-05-28 position-model gate fix:

1. **`_tick()` pickup query (line 592)** filtered intents on `holds_executor_seat=True` — the post-time flag indicating the POSTER held the seat when they emitted the intent. So REDEYE's crypto BUYs (posted while Alpha held the equity executor seat) were never picked up by the auto-router, even though the gate would now pass them.

2. **`_sweep_seat_mismatched_intents()` (line 484)** ran every 30s and actively destroyed intents where `holds_executor_seat=False`, stamping them `gate_state=blocked` with the legacy brain-coupled reason "intent posted when seat held by X, not Y — terminal". A silent garbage collector that killed intents the position-model gate would have passed. **This is the actual mechanism that severed the line.**

Both surfaces asked the wrong (brain-coupled) question. The gate now asks "does ANY brain currently hold the executor seat for this lane?" — these two surfaces still asked "did the POSTER hold the seat at post-time?"

### Shipped

1. **`_sweep_seat_mismatched_intents()` rewrite** (`shared/auto_router.py:478-577`): now position-model. Iterates pending cross-brain intents; for each, checks via `get_seat_holder` + `seats_with_execute` whether ANY brain currently holds an execute-capable seat for the intent's lane. If yes → leaves the intent pending. If no holder anywhere → terminally blocks with a typed lane-aware reason ("no current executor-seat holder for lane=X"). Per-lane occupancy cached for the sweep duration to avoid hammering the seat collection.

2. **`_tick()` pickup query relaxation** (`shared/auto_router.py:579-665`): removed the `holds_executor_seat=True` filter from the mongo query. Replaced with a per-intent position-model eligibility check using the same `get_seat_holder` + `seats_with_execute` logic. Samples up to 4× the per-tick max so the in-memory filter can drop ineligible intents and still leave us with up-to-MAX_PER_TICK eligible ones.

3. **New endpoint** `POST /api/admin/intents/resurrect-position-model-victims` (`shared/intents.py:1402+`): operator-only one-shot to flip intents wrongly terminated by the OLD sweep back to `gate_state=pending` so they get re-evaluated under the position-model gate. Idempotent: only resurrects intents whose latest gate-result row carries the legacy marker "swept by auto_router seat-mismatch cleanup". `dry_run=true` by default — returns counts without mutating.

4. **4 new tests** in `tests/test_auto_router_position_model.py`:
   - Sweep LEAVES a cross-brain intent pending when the lane has a current holder (doctrine-critical: the case that was broken).
   - Sweep TERMINALLY BLOCKS when the lane has no holder anywhere (the gate would fail it too).
   - Block reason names the new doctrine ("no current executor-seat holder for lane=X"), not the old brain-coupled phrasing.
   - Already-executed intents are never re-swept (belt-and-braces).

### Verified
- All 4 sweep tests pass on preview.
- Live endpoint `POST /api/admin/intents/resurrect-position-model-victims?dry_run=true&limit=50` on preview: found 50 candidates, all dry-run-resurrectable. Reports zero false positives (`skipped_blocked_by_other_reason: 0`, `no_longer_blocked: 0`).
- Lint clean on `auto_router.py` and `intents.py`.

### Operator workflow on prod after redeploy

```bash
# 1. Preview how many intents the OLD sweep wrongly killed:
curl -X POST "https://mission.risedual.ai/api/admin/intents/resurrect-position-model-victims?dry_run=true&limit=1000" \
  -H "Authorization: Bearer $TOKEN" | jq .

# 2. If the count looks right, flip them back to pending:
curl -X POST "https://mission.risedual.ai/api/admin/intents/resurrect-position-model-victims?dry_run=false&limit=1000" \
  -H "Authorization: Bearer $TOKEN" | jq .

# 3. The next auto-router tick (every AUTO_ROUTER_INTERVAL_SEC seconds)
#    will re-run the position-model gate chain on each resurrected
#    intent and route the ones that pass to the broker.
```

### What this does NOT change
- Patent J REJECT label still keeps low-quality intents from auto-routing (doctrine quality is its own knob — pass #36 demoted the redundant `execution_judge` chip but didn't change the doctrine threshold).
- Lane execution toggles still need to be enabled on prod for the auto-router to actually fire vs observe. If `lane_execution_toggles` for equity / crypto are not flipped, the auto-router stays in observation mode.
- `may_execute=False` is still pinned at the intent ingest validator — Doctrine (c) is intact. The execution wire mints authority via receipts inside `broker_router.route_order`, never as a brain-set field.

---


## 2026-05-31 (pass #36) — execution_judge demoted to advisory-only `setup_quality_summary`

### Operator finding
The operator queried "where did `execution_judge` come from? It wasn't in my original design." Git archaeology found it was introduced 2026-05-17 by a prior agent (`commit 2273373`) as part of the Patent J doctrine sidecar — a role labeled "execution_judge" was attached to the doctrine packet alongside strategist/adversary/governor. It was NEVER in the operator's 4-seat doctrine (Strategist · Governor · Auditor · Executor) and was NEVER registered in `seat_policy.required_seats()` — yet the UI rendered it as a peer chip, visually implying execution authority.

Receipts of harmlessness: code audit confirms NO gate, NO auto-router decision, and NO broker call reads `execution_judge.execution_ready`. The scorecard reads it for analytics correlation only (does ready=True correlate with wins?). So the demotion is a pure UI + role-label change with zero behavioral risk.

### Shipped (Option B — rename + visual demotion)

1. **`shared/crypto/doctrine/crypto_brain_sidecars.py`**:
   - Renamed `_build_execution_judge` → `_build_setup_quality_summary` (legacy symbol kept as an alias for callers).
   - Role label changed: `"role": "execution_judge"` → `"role": "setup_quality_summary"`.
   - Added `"advisory_only": True` and `"blocks_execution": False` to the dict.
   - New canonical field `summary_ok` (was `execution_ready`); legacy `execution_ready` kept as deprecated alias for scorecard backward-compat.
   - `may_execute: False` + `may_create_direction: False` preserved (doctrine invariants on the role).

2. **`shared/doctrine/strategy_doctrines.py`** (equity gap-and-go + micro-pullback): same demotion on the equity packet — role label flipped, advisory pins added, packet KEY `"execution_judge"` retained so the scorecard's audit-row joins still work.

3. **`frontend/src/components/DoctrineStrip.jsx`** — visual demotion:
   - Removed `execution_judge` from the 4-role peer-chip array; strip now renders 3 real seats only.
   - New `SetupQualityBadge` component renders an inline neutral badge alongside the DOCTRINE pill: `setup ok` / `setup: <check_name>` / `setup: N checks failed`. Neutral grey color (never green/red authority colors).
   - New `SetupQualityDetail` component renders the per-check breakdown in the expandable details panel with an "ADVISORY ONLY · does not gate execution" footer banner.
   - Stale `seatHeadline(execution_judge, …)` switch branch reduced to a defensive fallback ("advisory") in case any caller still passes the legacy role string.

4. **`tests/test_execution_judge_demotion_lock.py`** — new doctrine-lock with 5 pins so a future agent cannot silently re-promote:
   - `execution_judge` MUST NOT be in `required_seats()`.
   - Crypto packet's `role` MUST be `"setup_quality_summary"` with `advisory_only=True, blocks_execution=False`.
   - Both equity packets (gap-and-go + micro-pullback) MUST same.
   - `execution_ready` alias retained for scorecard backward-compat (`summary_ok` and `execution_ready` always agree).

5. **`tests/test_strategy_doctrines.py::test_strategy_packet_uses_same_role_keyed_shape`** — updated to assert the new role-label contract while still pinning the demoted role's authority invariants.

### Verified
- 42 doctrine-related tests pass (12 new demotion + judge-surfacing tests, plus the existing strategy/crypto/sidecar suites).
- Live UI screenshot at `/admin/intents`: three real seat chips, neutral inline `SETUP: N CHECKS FAILED` badge, no `EXECUTION JUDGE` peer chip. Detail-card expansion preserved.
- 4 pre-existing failures (`test_doctrine_intent_attachment`, `test_doctrine_outcome_join_and_scorecard`) are P3-backlog drift unrelated to this pass — confirmed by stash+rerun on `main`.

### Doctrine result
The operator's original 4-seat doctrine is restored visually. Patent J's quality verdict is still computed and audited — it's just no longer dressed as a fifth seat.

---


## 2026-05-30 (pass #35) — Alpaca auto-pinger (close the 17h staleness gap)

### Operator finding
On prod, broker telemetry showed `ALPACA LAST PING 17h ago` (red ✗) while Kraken showed `LAST POLL 22s ago` (green). Investigation revealed MC has NO auto-pinger for Alpaca — `last_ping_at` was only updated when somebody (operator or a brain) called `POST /api/admin/alpaca/test` manually. Kraken's poller refreshes the equivalent stamp naturally every 60s as a side-effect of its OHLCV pull loop. Alpha agent (correctly) flagged this as an MC-side issue; the doctrine point: both brokers' credentials live in MC, so both should have symmetric liveness loops.

This is also the same disease as RedEye's 7-hour gap from earlier this session — a missing scheduler, not a broken worker.

### Shipped
1. **`shared/broker/alpaca_routes.py`** — added auto-pinger task. Mirrors Kraken's poller pattern (`_pinger_tick`, `_pinger_loop`, `start_pinger_if_needed`, `stop_pinger`). Every `ALPACA_PING_INTERVAL_SEC` (default 120s, configurable via env) calls `adapter.ping()` and refreshes the same fields the manual `/test` endpoint touches: `last_ping_at`, `last_ping_ok`, `last_equity_snapshot`, `last_ping_error`.
   - Fail-soft: Alpaca outage → stamps `last_ping_ok=False` + error, loop continues to next tick.
   - No-op when credentials are missing (preview state) — side surface reports `no_credentials` so operators can distinguish "broker down" from "broker not connected".

2. **`shared/broker/alpaca_routes.py::GET /pinger/status`** — operator-visible health surface (`task_alive`, `interval_sec`, `last_tick`). Distinct from `/status` (which surfaces the broker's own `last_ping_at`); this answers "is the auto-pinger itself healthy" — same role as Kraken's `_POLLER_LAST_TICK`.

3. **`server.py`** — boot wires `start_alpaca_pinger_if_needed()` after `start_auto_router_if_enabled()`. Shutdown calls `stop_alpaca_pinger()` alongside the other lifecycle teardowns. Safe no-op when creds missing.

4. **`tests/test_alpaca_pinger.py`** — 5 tests:
   - Tick refreshes `last_ping_at`, `last_ping_ok=True`, equity snapshot, clears error on success.
   - Tick stamps `last_ping_ok=False` + error on Alpaca failure WITHOUT raising (loop survival).
   - Tick no-ops when credentials missing (no exception, side stamp = `no_credentials`).
   - `start_pinger_if_needed` is idempotent (no double-spawn on lifespan reloads).
   - Loop swallows tick exceptions and continues iterating.

### Verified live (preview)
- Boot log: `risedual.alpaca_pinger - INFO - alpaca auto-pinger STARTED — every 120s`
- `GET /api/admin/alpaca/pinger/status` → `{task_alive: true, last_tick: {error: "no_credentials"}}`
- On prod (with creds connected), `last_ping_at` will refresh every ≤120s automatically.

### What this is NOT
This pass deliberately does NOT address Alpha's separate "should crypto trades flow through MC for audit lineage" question. That's a doctrine call (crypto authority model), not a plumbing bug. Pending operator steer.

---


## 2026-05-30 (pass #34) — Brain status tile surfaces RedEye's new check-in identity fields

### Operator directive
RedEye agent shipped both new identity surfaces (`mc_url_set`/`ingest_token_set` for check-in, `mc_base_url_set`/`redeye_ingest_token_set` for heartbeat, plus the composite `checkin_worker_eligible` boolean) plus a "STARTED|NOT STARTED" lifecycle log line. Goal: turn the prod 7-hour-gap diagnostic from "curl + grep" into "look at the chip on the runtime page."

### Audit on MC side
- MC's proxy at `routes/brain_runtime.py::_fetch_upstream` reads `resp.json()` verbatim into `payload` — new fields flow through unmodified on prod. No proxy code change needed.
- Preview MC has no `REDEYE_STATUS_URL` configured (expected for dev passthrough) — the proxy correctly returns `no_upstream_configured` and the dashboard tile renders the graceful degraded state. End-to-end will be live on prod once MC redeploys.

### Shipped
1. **`frontend/src/components/BrainProxiedStatusTile.jsx`** — Identity section now:
   - Renders a top-line chip (green/red/grey dot + ELIGIBLE/NOT ELIGIBLE/unknown label) for `payload.identity.checkin_worker_eligible`. Sits above the KV rows so the operator sees the composite verdict first.
   - Adds two new KV rows for the heartbeat pair: `mc_base_url_set`, `redeye_ingest_token_set`. Keeps the existing check-in pair (`mc_url_set`, `ingest_token_set`).
   - Each row carries a stable `data-testid` so the testing agent / operator can target individual booleans.

### Diagnostic flow now
1. Operator opens `/admin/runtime/redeye` on prod.
2. Chip color answers the question in <1s:
   - **GREEN ELIGIBLE** → worker IS running. If the 7h gap persists, the issue is downstream (MC dedup, network blip, silent 5xx). Need: grep prod logs for `mc_checkin: periodic ping failed`.
   - **RED NOT ELIGIBLE** → worker never started. Look at the 4 boolean rows directly under the chip — whichever is `false` names the missing env var.
   - **GREY unknown** → sidecar is older than the new spec; ship the upgrade or fall back to grep.

### Doctrine fit
The tile remains purely observational (operator-read-only, proxy auditing every call to `brain_status_proxy_audit`). Nothing about authority, gates, or seat assignment changes.

---


## 2026-05-30 (pass #33) — Seat-holder nudges (operator → silent seat)

### Operator directive
Confirmed the improvement proposed at end of pass #32: a one-click "Notify holder" action on each missing seat in the Positions quorum stripe — fires a typed nudge to the brain currently in that chair via the runtime-token channel, with cooldown.

### Shipped
1. **`shared/seat_nudges.py`** — new module with three endpoints:
   - `POST /api/admin/positions/{position_id}/nudge-seat` — operator pings the brain currently holding `seat`. Resolves the address at SEND time from the live roster (position-model purity: same brain that quorum considers "engaged" is the brain that gets the nudge). 30-min cooldown per (position, seat). Returns 422 unknown seat, 404 vacant or missing position, 429 cooldown with `retry_after_seconds`.
   - `GET /api/admin/positions/{position_id}/nudges` — operator reads history.
   - `GET /api/runtime-discussion/seat-nudges?runtime={brain}&since={iso}` — brain-callable via runtime-token. Poll-friendly with `since` cursor.

2. **`namespaces.py`** — `SEAT_NUDGES = "seat_nudges"` (append-only collection).

3. **`server.py`** — `seat_nudges_router` wired into `/api`.

4. **`frontend/src/pages/Positions.jsx`** — `SeatNudgeRow` component renders inside the quorum stripe. For each missing seat with a current holder, a `↗ NUDGE <BRAIN> /<seat>` button. For vacant required seats, a dashed-border `VACANT /<seat>` chip (no one to ping). Sonner toast on success / typed cooldown message on 429. Page-level roster fetch parallels positions fetch on every 15s poll so newly-rotated holders are addressed correctly on the next render.

5. **`tests/test_seat_nudges.py`** — 7 tests cover: nudge addresses CURRENT holder (proves resolve-at-send-time), vacant seat 404, unknown seat 422, unknown position 404, cooldown 429, per-(position,seat) isolation, newest-first listing.

### Doctrine guard
The nudge endpoint is stamped `authority: "advisory_observability_only"` in every row and the docstring asserts:
- does NOT force a seat reassignment
- does NOT veto an intent
- does NOT modify execution authority
- does NOT affect any gate decision

Brain pulls via poll — MC never pushes / retries / escalates. Operator can chain nudges with cooldown gaps. Same pattern as `opinion_silence_watchdog`.

### Verified
- All 7 backend tests pass.
- Live curl confirmed: first nudge → 200 with brain=chevelle (current governor holder); immediate retry → 429 with retry_after_seconds=1799.
- UI screenshot at `/admin/positions` shows the stripe with both nudge buttons (NUDGE CAMARO /STRATEGIST, NUDGE CHEVELLE /GOVERNOR, NUDGE ALPHA /EXECUTOR where applicable) and VACANT chips for unfilled required seats.
- Live click recorded a row in `seat_nudges` with seat=strategist, brain=camaro — proving the address resolves through the live roster, not from any stale stamp.

---


## 2026-05-30 (pass #32) — Position-model quorum for strategist / auditor / all required seats

### Operator directive
*"Do what is necessary to get these seats inline with the doctrine."* — for auditor seat and strategist seat, following the executor-seat position-only relaxation in pass #31.

### Audit findings (read-only sweep)
| Seat | Where it's checked | Brain-coupled? |
|---|---|---|
| executor | `_evaluate_gates` `executor_seat_check` | Was coupled → fixed in pass #31 |
| governor | `_latest_governor_call`, `_governance_verdict` | Already position-model — `_seat_holder("governor", lane)` resolves current holder, then queries that brain's contributions |
| opponent / auditor | `_evaluate_opponent_gate` (council.py:663) | Already position-model AND advisory-never-blocks |
| strategist | Doctrine packet `fetch_seat_holders`, runtime profile overlay (`doctrine_routes.py:96`) | Already position-model |
| **quorum** | `_compute_quorum` (positions.py:227) | **WAS brain-coupled via `posted_as`** — fixed in this pass |
| opinion-silence watchdog | `routes/opinion_silence_watchdog.py:118` | Already position-model — iterates current roster |

### The doctrine bug `_compute_quorum` was hiding
The old implementation called a seat "engaged" if ANY historical stance carried `posted_as=<seat>`. A stance written by Camaro under `strategist`, then a rotation to Alpha → Camaro's residue still counted as the strategist seat being engaged, silently satisfying quorum on Alpha's behalf. Same brain-coupling family as the executor-seat-check bug: "the seat is engaged because the prior brain spoke under it" ≠ "the seat is engaged because the current authority spoke."

### Shipped
1. **`shared/positions.py::_compute_quorum`** — rewritten to position-model. A required seat is "engaged" iff `roster_assignments[seat]` exists AND that brain is in `stances_by_brain`. After rotation, prior-holder stances no longer satisfy the new holder's quorum. `stances_by_seat` continues to be exposed in the response for the UI's "what was last said under each seat" history view, but quorum no longer reads it.

2. **`shared/positions.py::_hydrate`** — passes `stances_by_brain` to `_compute_quorum`. `stances_by_seat` becomes display-only history; doctrine comment added.

3. **`tests/test_quorum_position_model.py`** — 7 new pure-function tests covering:
   - seat engaged when current holder stanced
   - seat MISSING when current holder silent even if predecessor spoke (the doctrine-critical case)
   - vacant required seats correctly flagged in both `vacant_required_seats` AND `seats_missing`
   - one brain holding multiple required seats engages both via a single stance
   - degraded flag correctness
   - governance_blindness clears when current governor speaks
   - governance_blindness PERSISTS after rotation if new governor silent (doctrine teeth)

### Verified
- All 7 new quorum tests pass.
- Live `/api/shared/positions` returns position-model correct payloads: e.g., `engaged=['executor']` (only alpha — current executor — stanced), `missing=['strategist','governor','opponent','auditor',...]` (current holders of these seats haven't stanced this position), `vacant=['opponent','auditor','crypto_auditor','crypto']` (no current holder).
- Lint clean on `shared/positions.py`.

### Operator visibility
The Positions page (`/admin/positions`) "missing seats" stripe now accurately reflects the **current** holders' silence, not stale historical engagement. After rotation, freshly-vacant authority is visible immediately — the new holder must re-stance to clear quorum.

---


## 2026-05-30 (pass #31) — Position-model executor seat + last-block-reason diagnostic

### Operator directive
*"There shouldn't be any seat permanently assigned to a brain. Restrict to the position not the brain."* — after seat-rotation experiment failed to unblock trading; data showed brain-coupling in `executor_seat_check`.

### Findings (preview DB, last 72h)
- 89 routable intents (BUY/SELL/SHORT/COVER) emitted. **0 passed, 100% blocked.**
- 1491 HOLD intents marked `dry_run_blocked` — these are watchlist signals, not trade attempts (false noise).
- First-failing-gate breakdown for routable intents:
  - `broker_connected`: 66 (camaro equity, Alpaca adapter = None on preview)
  - `executor_seat_check`: 23 (alpha/redeye/camaro crypto, wrong-brain or vacant)
- `may_execute pinned False` was misread as a block reason; it's gate-1's PASS message. Authority is in the receipt minted by `broker_router.route_order` after gates pass, not in any mutable intent field.

### Doctrine correction
The `executor_seat_check` gate was brain-coupled: required `holder == intent.stack` AND `executor_holder_at_post == intent.stack`. This made seat rotation useless — pending intents emitted while Camaro held the seat could not execute after the operator swapped to Alpha. Doctrine restated by operator (2026-05-30): **authority lives in the seat, not the brain. Whichever brain currently holds an execute-capable seat for the intent's lane has routing authority. Brain that posted is informational only.**

### Shipped
1. **`shared/execution.py` `_evaluate_gates` — position-model seat check.** Drop `holder == intent_stack` and `held_at_post == intent_stack` couplings. Gate now passes iff (a) some brain currently holds an execute-capable seat for the lane AND (b) that seat's policy permits the lane. `holds_executor_seat` / `executor_holder_at_post` continue to be stamped on intents for the audit trail but no longer participate in the gate decision.

2. **New endpoint: `GET /api/admin/execution/last-block-reason`** — read-only diagnostic. Returns the last N (default 20, max 100) blocked intents with first failing gate name + reason, plus a `summary_by_failing_gate` aggregation. Query params: `stack` (optional), `limit`, `include_hold` (default false — HOLDs are excluded to surface only true trade attempts).

3. **`RuntimeDetail.jsx` — "Last 20 blocked routable intents" card.** Renders the diagnostic above the decision log on every brain's runtime page. Shows summary chips (`N × gate_name`) plus per-intent rows: when, symbol, action, lane, failing gate, reason.

4. **`tests/test_execution_gates.py`** — renamed `test_stale_seat_blocks_after_rotation` → `test_seat_rotation_does_not_block_under_position_model`. Now asserts Camaro's pending intent passes the seat gate when Alpha currently holds the executor seat. Also fixed `_intent` fixture to include `lane="equity"` so newer lane-aware gates can evaluate.

5. **`tests/test_last_block_reason.py`** — 4 new tests covering: HOLD exclusion by default, first-failing-gate surfacing, `include_hold=true` opt-in, and summary count aggregation. Uses a unique fixture stack name to isolate from real DB rows.

### Verified
- All 4 last-block-reason tests pass; position-model test passes.
- Live endpoint returns real data on preview (summary: 19 × executor_seat_check, 1 × broker_connected for alpha).
- UI card renders correctly with summary chips and per-row reasons at `/admin/runtime/alpha`.

### Operator follow-up
Historical `dry_run_blocked` intents stamped with the old brain-coupled reason ("held by camaro at post time, not alpha") will now PASS the seat gate under the new doctrine — but their cached `shared_gate_results` rows still show the old reason text. Operator can re-evaluate them by calling `POST /api/admin/intents/auto-dry-run-drain` which re-runs the chain. Production preview is currently blocked by infrastructure (Alpaca adapter = None, executor seat empty, lane toggle off) — not by the seat-check doctrine. Plumbing must be filled before trades fire.

---


## 2026-02-17 (pass #29) — P3 test-fixture staleness + decider-alias doctrine lock

### Operator directive
*"P3 definitely need to be resolved."*

### Findings
1. **Test-fixture staleness was real and big**: 73 tests failing on `main`. 31 of them were in `test_risedual_backend.py` and all caused by 3 distinct drifts:
   - `deploy_mode == "observation"` assertions vs prod now in `"execution"` after the live-trading flip
   - Per-runtime `mode == "observation"` vs current `"seat-governed"` (different semantic, repurposed field)
   - Schema drift on `/api/admin/flags` (`enforce_flags` is now `{}`) and `/api/shared/receipts` (legacy `observed`/`executed` fields retired, replaced by discussion-layer `receipt_id`/`thread_root` shape) and `/api/runtime/{brain}/status` (`phase6_enforce_enabled`/`executor_enforce_enabled`/`authority_enabled` removed under seat-governed authority)

2. **The "strip `decider` paths" cleanup item was UNSAFE as written.** Live DB safety check:
   - `sovereign_audit_log`: 5,463 rows total, **1,363 (25%) contain legacy `decider` keys**
   - The alias-rewrite layer in `shared/roster.py:_LEGACY_ROLE_REWRITES` is LOAD-BEARING for historical audit reads
   - Stripping it would corrupt ~25% of MC's audit-log read responses

3. **Remaining 42 failures across `test_roster.py`, `test_seat_aliases.py`, `test_sovereign.py`, etc.** are pre-existing test/code drift unrelated to this session. Verified via `git stash` round-trip — same 42 fail on `main` without my changes.

### Shipped
1. **`test_risedual_backend.py`** — 31 stale assertions fixed:
   - Introduced `VALID_DEPLOY_MODES = {observation, execution}` and `VALID_RUNTIME_MODES = {observation, execution, seat-governed}` (the two were always different semantics; the test suite mixed them)
   - Operator-flippable booleans (`broker_live_order_enabled`, legacy enforce flags) are presence-checked only — value depends on current operator state
   - Receipts test accepts BOTH the legacy decision-log shape (`id`/`action`/`executed`) and the new discussion-layer shape (`receipt_id`/`thread_root`/`topic`)
   - Per-runtime `mode` is checked against `VALID_RUNTIME_MODES` — `phase6_enforce_enabled` etc removed (deprecated under seat-governed authority)

2. **`shared/roster.py`** — added explicit DOCTRINE PIN block above `_LEGACY_ROLE_REWRITES` documenting the 25%-audit-rows finding and warning future agents that the alias dict is mandatory.

3. **`tests/test_legacy_role_alias_doctrine.py`** — 6 new tripwires that fail if `_LEGACY_ROLE_REWRITES` is deleted, `decider`/`opponent` aliases are removed, or `_canonical_role` is rewritten to hardcode translations instead of reading the table.

### Results
- 38/38 PASS in `test_risedual_backend.py` (was 5 failing → 0)
- 6/6 PASS in `test_legacy_role_alias_doctrine.py` (new)
- Full-suite net: 73 failing → 42 failing (-31). Zero regressions introduced.
- Remaining 42 are pre-existing drift across roster / seat-aliases / sovereign — each requires per-test forensics, not a blanket fix. Recommend they be triaged separately if/when they block specific work.

### What did NOT ship (and why)
- **`decider` path strip** — refused on safety. Replaced with a doctrine pin + 6 tripwires that lock the alias layer against future "cleanup" attempts. The shims may only be removed AFTER a one-shot DB migration backfills canonical keys across every collection that ever stored a role/seat/posted_as field. That migration is its own multi-step pass, not a routine cleanup.
- **RedEye broker code removal** — not MC's responsibility (lives in RedEye's repo; RedEye author already working on it per their prior message).

### Next Action Items
- 🟢 **Operator** — redeploy MC. This pass is test-only + doctrine-comment; zero behavioral change. Net effect: green test bar reflects current production state (live trading flipped on, seat-governed authority active).
- 🟡 P1 — Polygon/Finnhub bar consumption + `has_news` indicator (MC endpoint shipped pass #25; awaiting brain wire-up)
- 🟡 P1 — R:R Scanner Phase C/D
- 🟡 P1 — Phase 3 cross-Shelly federation HTTP bridge

### Future / Backlog
- 🟢 P2 — Brain-side: investigate fleet-wide heartbeat drops (all 4 brains went DEAD simultaneously; cluster-level event, not Camaro-specific)
- 🟢 P2 — Investigate remaining 42 pre-existing test failures (each requires forensics)
- 🟢 P3 — One-shot migration to backfill canonical role keys across `sovereign_audit_log` and adjacent collections (only after which the alias layer can be removed)

---


## 2026-02-17 (pass #28) — Dual-sign removal completed (was security theater) + investigation finding on "the quiet"

### Operator decision
Operator: *"Can we get rid of the co-signing, it's only me remember?"*

### Backstory
2026-05-26 pass marked dual-sign as removed in `shared/promotion.py:13-19` doctrine comment, but the actual `required_signatures = 2 if target_authority == "primary" else 1` line at the proposal-creation path was NEVER changed. Existing in-flight proposals stored `required_signatures: 2`, the frontend rendered a `DUAL-SIGN` badge + 2-of-2 button labels, and the operator's prod dashboard still showed Alpha's pending `co_trader → primary` proposal stuck at `0/2` signatures from 2026-05-20 — perpetually un-countersignable.

### Shipped
1. **`shared/promotion.py:propose_from_latest_artifact`** — hard-codes `required_signatures = 1` for ALL ladder tiers. No conditional branch on target.
2. **`shared/promotion.py:list_proposals`** — self-healing migration: any legacy `required_signatures > 1` row in pending or `awaiting_second_sign` state is normalised to 1 on every read. Idempotent, safe to call.
3. **Deleted duplicate `reject` route** — pre-existing F811 error in source (two identical `@router.post("/{proposal_id}/reject")` blocks). One removed.
4. **`frontend/src/pages/Promotion.jsx`** — stripped `DUAL-SIGN` badge, "1st of 2 / Co-sign & elevate / waiting on a second operator" UX. Single button: "Countersign & elevate". `const required = 1;` hardcoded so a stale cache can't display `0/2`.
5. **6 doctrine tripwires** in `test_single_sign_promotion.py` — source-scan locks against re-introducing dual-sign anywhere (backend OR frontend).

### Live verified on preview
- `/admin/promotion` page renders cleanly
- `dual_sign_badges_on_page = 0` (no DUAL-SIGN label rendered anywhere)
- 6/6 tripwires green

### "The quiet" — investigation finding
Operator asked whether Patent J FAIL might cascade into the trading path and cause silent intent suppression.

**Answer: NO.** Patent J readiness is consulted in exactly two places, both inside `shared/promotion.py` (`propose_from_latest_artifact` + `readiness_now`). The execution gate chain (`shared/execution.py`), intent processing (`shared/intents.py`), and council orchestration (`shared/council.py`) NEVER read it. Patent J FAIL only blocks AUTHORITY ELEVATION; it does not affect trading.

**Actual reasons for the quiet** (unchanged from prior passes):
- Camaro's heartbeat dies recurrently → no strategist BUY/SELL → Alpha has nothing to execute
- RedEye decision_log = 0 → no governor stance updates → opinion-staleness gate (pass #26) hard-blocks at 30min stale
- Chevelle DEAD 3h on prod → same staleness gate fires

### Next Action Items
- 🟢 **Operator** — redeploy MC. On first dashboard load post-redeploy, the legacy `0/2` proposals will self-heal to `0/1` and become countersignable.
- 🟡 P1 — Polygon/Finnhub bar consumption + `has_news` indicator
- 🟡 P1 — R:R Scanner Phase C/D

---


## 2026-02-17 (pass #27) — Backlog cleanup: 6-Brain Expansion Refactor SHELVED PERMANENTLY

### Operator decision
Operator: *"You can get rid of the 6 brains idea. Just shelf it permanently."*

### Shipped
- Renamed `/app/memory/SIX_BRAIN_REFACTOR_PLAN.md` → `/app/memory/SHELVED_SIX_BRAIN_REFACTOR_PLAN.md`
- Prepended a `SHELVED PERMANENTLY 2026-02-17` banner at the top warning future agents not to revive or implement. File preserved for archaeological reference only.
- Removed the in-source breadcrumb in `tests/test_shelly_pipeline.py:test_pipeline_auto_extends_with_live_runtimes` docstring (was the only test-suite reference). The test contract still holds — shelly pipeline must auto-extend with LIVE_RUNTIMES regardless of future roster changes — but no longer points at the dead plan.

### What this means going forward
Brain roster stays at 4 (Alpha, Camaro, Chevelle, RedEye). If the roster ever needs to grow, the work should be designed from first principles against the live codebase, NOT by reviving the shelved plan (which predates several doctrine passes: sovereign mode guard, seat-policy hardening, governor exclusivity).

### Next Action Items (post-shelving)
- 🟡 P1 — Real `relative_volume` + Polygon/Finnhub bar consumption (MC endpoint shipped pass #25; awaiting brain wire-up)
- 🟡 P1 — R:R Scanner Phase C/D (tiered cache + strict 5:1 enforcement)
- 🟡 P1 — Phase 3 cross-Shelly federation HTTP bridge

### Future / Backlog
- 🟢 P2 — Pre-existing `test_quorum_and_provenance::test_governor_silent_flags_governance_blindness` failure
- 🟢 P2 — SSE stream `/api/mc-connection/stream` for live dashboard
- 🟢 P2 — Pulse review-queue UI for Governance Reviewer
- 🟢 P3 — Cleanup: legacy `decider` paths, dead RedEye broker code, stale `deploy_mode == "observation"` fixtures

---


## 2026-02-17 (pass #26) — Opinion-staleness gate hardening + executor seat doctrine in brain-health tile

### The loophole
`shared/council.py:_resolve_governor_context` set `governor_alive = True` unconditionally whenever `gov_norm` (the governor's normalized stance for a symbol) was non-None. A 6h-old stance kept the governor gate "live" forever — allowing intents to fire through a long-dead governor's cached opinion. Operator caught this on prod when Chevelle's 3h-stale `neutral @ conf 0.00` was still satisfying the governor-quorum on Alpha's intents.

### Shipped

1. **Council-side fix** — `_resolve_governor_context` now applies `_is_fresh(gov_norm.ts, _GOVERNOR_OFFLINE_THRESHOLD_SECONDS)` to the stance itself. A stale stance is treated as `gov_norm = None` AND `governor_alive = False`, routing into the existing GOVERNOR_OFFLINE → hard-block path. Boundary tested at 29min (fresh) and 31min (stale).

2. **Brain-health tile** — executor / crypto-executor seats are no longer flagged for opinion-silence:
   - Backend `_compute_overall` checks `opinion_producing_seat_roles = {strategist, governor, auditor, advisor}` and only flags silence when one of those is held.
   - Frontend `BrainHealthTile` shows `OPINION: n/a (executor)` with neutral dot + tooltip explaining "Executor seats route orders; they do not post opinions."
   - Counter-test included: a brain holding `strategist` is STILL flagged on silence (exemption is per-role, not blanket).

### Tripwires
- 5 new in `test_governor_staleness_gate.py` — boundary test at 30min threshold; fresh stance pass-through; source-scan invariant against re-introducing the unconditional `governor_alive = True` pattern.
- 2 new in `test_brain_health.py` — executor-only exemption + strategist counter-test.
- Pre-existing test `test_quorum_and_provenance::test_governor_silent_flags_governance_blindness` fails on main with or without my changes (verified via `git stash` round-trip). Unrelated to this pass.

### Operator pattern
**Before:** Chevelle DEAD 3h → her last cached `neutral @ conf 0.00` keeps satisfying governor-quorum → Alpha fires intents on a dead governor's stale opinion.

**After:** Chevelle's stance ages past 30min → `gov_norm = None` + `governor_alive = False` → `_governance_verdict` emits `GOVERNOR_OFFLINE` → hard block. Same behavior as if the governor never opined. Fail-closed.

### Operator pattern (UI)
**Before:** Alpha (executor) shows `OPINION: NEVER` → operator thinks Alpha is broken.

**After:** Alpha shows `OPINION: n/a (executor)` with dimmed dot → operator immediately sees this is expected behavior.

### Next Action Items
- 🟢 **Operator** — redeploy MC. Both fixes ship together (one council edit + one brain-health edit + one frontend label edit). Net effect on prod: any 30min+ stale governor stance starts hard-blocking trades instead of silently passing them.
- 🟡 P1 — 6-Brain Expansion Refactor (deferred)
- 🟡 P1 — R:R Scanner Phase C/D

---


## 2026-02-17 (pass #25) — Feature service + brain-callable roster + status proxy

### Shipped (one-shot for the next MC redeploy)

1. **`shared/market_data/feature_service.py`** — derives `relative_volume` + `has_news` from MC's existing `shared_ohlcv_bars` collection + Finnhub news API.
   - Doctrine pin: `relative_volume = None` (NOT 0.0) when bars insufficient → prevents false-positive `STUCK_FEATURES_NO_DIVERSITY` self-vetoes downstream.
   - `has_news = None` on Finnhub failure (missing key, timeout, error-dict response); only `False` on successful empty fetch.
   - In-process news cache TTL 300s, operator-tunable.

2. **`routes/market_data_snapshot.py`** — operator + brain dual-auth.
   - `GET /api/admin/market-data/snapshot/{symbol}`
   - `GET /api/admin/market-data/snapshot?symbols=NVDA,AAPL,TSLA` (batch ≤50, per-symbol error isolation)
   - `POST /api/admin/market-data/snapshot/cache/reset-news` (operator escape hatch)

3. **`routes/brain_runtime.py`** — three brain-callable + operator endpoints.
   - `GET /api/admin/runtime/roster?caller={brain}` — brain-callable lean roster (dual auth). Returns `your_seats` lane-resolved + full `assignments` map. Brain caller is FORCED to its authenticated brain id (can't peek at another brain's seats by passing `?caller=other`). Doctrine-compatible by being read-only — governor exclusivity is enforced at write time in `shared/roster.py:_ensure_assignment_eligible`.
   - `GET /api/admin/runtime/{brain}/status` — operator-only status PROXY. Fetches `<BRAIN>_STATUS_URL` env var, bounded timeout 4s, cached 10s, returns `{ok, payload}` wrapper. Brain pods can ship a `/status` endpoint per RedEye's wire-up kit and operator dashboard surfaces it without cross-origin pain.
   - `POST /api/admin/runtime/{brain}/status/refresh` — operator force-refresh.
   - `GET /api/admin/runtime/status-proxy-audit` — operator forensics on proxy hits/misses.
   - Every proxy call writes one row to `brain_status_proxy_audit` (success AND failure).

4. **`components/BrainProxiedStatusTile.jsx`** — renders the proxied brain payload on `/admin/runtime/{brain}` page. 7-section grid (identity, seats, heartbeat, governor_emitter, data_keys, neuro_engine, intents) — each section silently no-ops when absent so different brains can expose different subsets. Cache badge, force-refresh button, graceful `no_upstream_configured` state with the env-var instructions inline.

### Tripwires (50/50 PASS across this session's modules)
- `test_market_data_feature_service.py` — 22 tests (RVOL math, news fallback contract, cache hit, broker-key abstinence, route auth)
- `test_brain_runtime.py` — 13 tests (roster lane-scoping, brain-caller can't peek, proxy timeout bound, audit-writes-every-attempt, governor doctrine compatibility)
- `test_brain_health.py` — 15 tests (still green from pass #23)

### Live verification on preview
- `/api/admin/market-data/snapshot/NVDA` → `{relative_volume: null, reason: "no_bars_for_symbol", has_news: null, reason: "finnhub_api_key_missing"}` ✅
- `/api/admin/runtime/roster?caller=redeye` → `seat_epoch=221, your_seats=[]` ✅ (redeye correctly unseated in preview)
- `/api/admin/runtime/redeye/status` → `{ok: false, error: "no_upstream_configured"}` ✅
- `/admin/runtime/redeye` page → tile renders `no_upstream_configured` state with env-var instructions; retry button + secondary graceful card both present.

### Next Action Items
- 🟢 **Operator** — redeploy MC. RedEye author is unblocked the moment this lands:
  - Set `REDEYE_MC_ROSTER_URL=https://mission.risedual.ai/api/admin/runtime/roster?caller=redeye` in RedEye's `.env` → their `redeye_seat_state.refresh_from_mc()` populates from authoritative source.
  - Set `REDEYE_STATUS_URL=https://redeye.risedual.ai/api/admin/runtime/redeye/status` in MC's `.env` → dashboard tile lights up green with brain-internal telemetry.
- 🟡 P1 — 6-Brain Expansion Refactor (deferred)
- 🟡 P1 — R:R Scanner Phase C/D

### Doctrine note
Operator reaffirmed Doctrine (c): *"The seat determines the pool permissions and restrictions not the brain. The only restrictions should be on the Governor seat for the two brains to be seated, RedEye and Chevelle."* MC's existing `shared/roster.py:_ensure_assignment_eligible` already enforces this (governor + crypto_governor exclusive to Chevelle/RedEye; everything else seat-based). My new brain-callable read endpoint is doctrine-compatible by abstinence — no write paths, locked by `test_roster_endpoint_doctrine_compatible`.

---


## 2026-02-17 (pass #24) — Brain-Health click-through + regression-only desktop notifications

### Shipped
1. **Card click-through** — every Brain-Health card is now a `<Link to="/admin/runtime/{brain}">` with hover/focus border highlight + ↗ glyph. Glance → click degraded card → forensics in one motion.
2. **Pre-existing crash fix in `RuntimeDetail.jsx`** (surfaced by the click-through):
   - `SUB_ENDPOINT[redeye]` was undefined → `Cannot read properties of undefined (reading 'url')` crash on any nav to `/admin/runtime/redeye`. Gated with `?.title` + `{sub && (...)}` wrap around the decision-log card.
   - Each `Promise.all` fetch wrapped in `.catch(() => ({data: null}))` so a single 404 (e.g. `/runtime/redeye/status` not present yet) can't tank the whole page.
   - New `loaded` state distinguishes "loading" from "fetched-but-no-status-endpoint" → graceful "No per-runtime status endpoint is wired for REDEYE" card pointing back to Diagnostics.
3. **Opt-in desktop notifications on regression** (`lib/brainHealthAlerts.js` + tile integration) with operator-pinned doctrine:
   - Fires ONLY on `green → degraded` or `green → dead`.
   - Does NOT fire on the inverse (any → green is recovery, not regression).
   - Does NOT fire on `degraded ↔ dead` flips (already broken; second ping is noise).
   - Does NOT fire on first-load (no prior verdict).
   - Per-brain 60s debounce — flapping pod cannot machine-gun the operator.
   - Persisted toggle in localStorage; explicit OS permission request on click; graceful "browser blocked" indicator when denied.
4. **17 doctrine tripwires** in `lib/__tests__/brainHealthAlerts.test.mjs` — pure-Node, no jsdom. Exercises every transition matrix cell + composite `computeRegressions(...)` + debounce window.

### Live verification on preview
- Click redeye card from Diagnostics → URL → `/admin/runtime/redeye` → page mounts cleanly (no error overlay, `runtime-page-redeye` testid present, graceful unavailable card visible).
- `○ ALERTS OFF` toggle renders next to `↻ REFRESH`; headless Chromium shows `browser blocked notifications` amber indicator (denied path working).
- All 4 brain cards still render correctly: Alpha exec×equity (2h), Chevelle gov×equity (2h), Camaro stra×equity (2m), Redeye fully null (no held seats).
- 17/17 alert tripwires pass.

### Next Action Items
- 🟢 **Operator** — redeploy MC. RedEye author is holding their redeploy until MC ships. No MC-side blocker remains.
- 🟡 P1 — 6-Brain Expansion Refactor
- 🟡 P1 — Real `relative_volume` via Kraken OHLC + Polygon/Finnhub bar consumption
- 🟡 P1 — R:R Scanner Phase C/D

---


## 2026-02-17 (pass #23) — Brain-Health composite endpoint + admin tile

### Operator pattern
Post-redeploy verification used to require three curls against three independent surfaces: sidecar-checkin / opinion-silence-watchdog / sovereign-audit-log walk per seat. This pass collapses that to ONE endpoint and ONE tile glance.

### Shipped
1. **`routes/brain_health.py`** — read-only composite. Two endpoints:
   - `GET /api/admin/runtime/brain-health/{brain}` — singleton
   - `GET /api/admin/runtime/brain-health` — fleet rollup (used by tile)
   - Joins `sidecar_checkins` + `shared_opinions` + `market_data_key_fetches` + `sovereign_audit_log`. Never writes.
2. **Doctrine-pinned thresholds in the payload** — `checkin_max_age_s=300`, `opinion_max_age_s=900`, `seat_walk_max_age_s=1800`. Operator's contract: tile + alerter + future LLM summariser all read the same numbers without grepping source.
3. **Lane-scoped seat-walk** — per `(role, lane)` cell: `{ts, age_sec, stale, mode, seat}` if the brain CURRENTLY holds the seat, `null` if not. A historical walk for a previously-held seat is filtered out by consulting the live roster. Operator's explicit ask: null = dimmed dot, not red.
4. **`overall.verdict`** — `green | degraded | dead` with `reasons[]` array (e.g. `checkin_dead_4221s`, `opinion_silent_5000s`, `governor_equity_stale_3600s`). A seatless brain that's opinion-silent is correctly GREEN (no seat → nothing to opine on).
5. **Frontend `BrainHealthTile.jsx`** — 4-card grid on `/admin/diagnostics`. Per brain: verdict dot, three signal rows (checkin/opinion/data-keys), seat-walk role × lane grid, "why" reasons. 15s auto-refresh.

### Tripwires (15 new in `tests/test_brain_health.py`)
- Thresholds present + sane in module-level constant
- Lane-seat map covers both lanes for every role
- Source-scan: no `.insert_*`/`.update_*`/`.delete_*` calls anywhere in the module (read-only enforcement)
- Source-scan: no broker key references (ALPACA_API_KEY / KRAKEN_SECRET / etc.)
- `_compute_overall`: green / degraded / dead branches; seatless-brain-opinion-silence is NOT degraded; null seat cells never generate reasons; thresholds always echoed
- Routes registered on documented paths + guarded by `get_current_user`
- `_gather_seat_walk` MUST call `get_roster` + filter via `held_seats` set (prevents historical-walk regression)

### Live verification on preview
```
$ curl /api/admin/runtime/brain-health
brains: ['alpha', 'camaro', 'chevelle', 'redeye']
  alpha:    verdict=dead  reasons=['checkin_dead_4221s']
  camaro:   verdict=dead  reasons=['checkin_dead_588889s']
  chevelle: verdict=dead  reasons=['checkin_never']
  redeye:   verdict=dead  reasons=['checkin_never']
```
All-dead is correct for preview (brains check into prod, not preview). Camaro's seat-walk correctly shows only `strategist × equity` populated; phantom `executor × equity` historical walk filtered out. Tile renders 4 cards with verdict dots, lane-scoped seat dots, threshold echo in header.

### Next Action Items
- 🟢 Operator: nothing to do for this pass. After RedEye redeploys (their torch decision), the tile turns green automatically.
- 🟡 P1 — 6-Brain Expansion Refactor per `SIX_BRAIN_REFACTOR_PLAN.md`
- 🟡 P1 — Real `relative_volume` via Kraken OHLC + Polygon/Finnhub bar consumption
- 🟡 P1 — R:R Scanner Phase C/D

---


## 2026-02-17 (pass #22) — Frontend AuthContext resilience: stop logging operators out on transient backend errors

### Bug
`/app/frontend/src/context/AuthContext.js` cleared the operator's token (`setToken(null)`) inside a bare `catch {}` around `/auth/me`. Any non-2xx response (5xx, 502 Cloudflare blip, network timeout, MC redeploy gap) bounced the operator to /login mid-incident-response. Recurring P1 in handoff. The user is on `mission.risedual.ai` (prod) — they hit this regularly during the live trading flip.

### Shipped
1. **`AuthContext.js` rewrite** —
   - New `AUTH_ERROR_STATUSES = new Set([401, 403])` — the ONLY statuses that purge the token.
   - New `RETRY_DELAYS_MS = [500, 1500, 3000]` — three retries with exponential-ish backoff (~5s patience window) before giving up.
   - New `isAuthRejection(err)` helper — gates the token clear behind an explicit status check; treats `err.response === null` (network failure) as transient.
   - On retry exhaustion: KEEP the token in localStorage so next page-load / refresh can re-auth once MC is healthy. User is shown /login (status=ready, user=null) rather than hanging on "Authenticating".

2. **5 new pytest tripwires** — `tests/test_frontend_auth_context_resilience.py`:
   - `AUTH_ERROR_STATUSES` must be exactly `{401, 403}` (no 5xx leakage).
   - `RETRY_DELAYS_MS` must exist with ≥500ms cumulative patience.
   - Forbids `catch { setToken(null) }` regression pattern via regex.
   - Every `setToken(null)` in the file must be reachable only from an `isAuthRejection` branch or the `logout` callback.

### Live verification on preview
- Logged in as `admin@risedual.io` → token minted ✅
- Intercepted `/auth/me` with synthetic `503` via Playwright `page.route` → reloaded `/admin/hypothesis` → **token survived in localStorage** ✅ (old code would have cleared it on the first 503)
- 401 path unchanged by design and locked by `test_isauthrejection_guards_token_clear`.

### Tripwire status
1385 backend tests collected, +5 new (frontend resilience). My JS-only change cannot affect backend pytest. Pre-existing 73 failures (e.g. `test_health_ok` asserting `deploy_mode == "observation"` while prod is now `"execution"` post-flip) are stale fixtures for the new live-trading state — unrelated to this change.

### Doctrine pins reinforced
- Operator session = scarce resource during incident response. Transient infra failure must NEVER be confused with auth rejection.
- Source-level invariants prevent silent regression (no jsdom dependency added).

### Next Action Items
- 🟡 P1 — 6-Brain Expansion Refactor per `SIX_BRAIN_REFACTOR_PLAN.md`
- 🟡 P1 — Real `relative_volume` via Kraken historical OHLC in MC labeler
- 🟡 P1 — Polygon/Finnhub bar consumption in `market_data_service.py`; `has_news` indicator
- 🟡 P1 — R:R Scanner Phase C/D (tiered cache + strict 5:1 enforcement)
- 🟡 P1 — Phase 3 cross-Shelly federation HTTP bridge
- 🟢 P2 — SSE stream `/api/mc-connection/stream` for live dashboard
- 🟢 P2 — Pulse review-queue UI for Governance Reviewer
- 🟢 P3 — Cleanup: legacy `decider` paths, dead RedEye broker code

---


## 2026-05-28 (pass #21) — Opinion-silent watchdog: bug fix + background scanner + tripwires

### Shipped
1. **Bug fix** — `routes/opinion_silence_watchdog.py::_last_opinion_age` was reading `created_at`, a field the opinion schema **never writes** (see `shared/opinions.py::post_opinion` which stores `posted_at`). The watchdog therefore reported every brain as "never posted" on every scan — false-positive flood. Now reads `posted_at`. Live `/status` now correctly shows camaro/alpha/chevelle ages in seconds.
2. **Background worker** — `shared/runtime/opinion_silence_worker.py`. Autonomous tick (default 15 min) runs the same `perform_scan(...)` the HTTP `/scan` endpoint uses → exactly ONE silence-detection code path. Doctrine-pinned advisory-only; cannot ever import broker/execution surfaces (locked by tripwire).
3. **New `GET /api/admin/opinion-silence-watchdog/status`** — UI-facing live silence picture without writing alerts. Returns `{seat, brain, age_sec, silent, kind}` per occupied seat.
4. **Refactor** — `scan()` HTTP endpoint now delegates to `perform_scan(...)`. Makes the worker + HTTP surface share one tested implementation.
5. **Lifespan wiring** — `server.py` starts the watchdog worker on boot, stops it on shutdown. Disabled cleanly via `OPINION_SILENCE_WATCHDOG_ENABLED=false`.
6. **Tripwires** — `tests/test_opinion_silence_watchdog.py` (14 tests): pins `posted_at` field read, vacant-seat skip, LIVE_RUNTIMES-only scope, cooldown throttling, stale-seat alert emission, worker start/stop idempotency, and `no_execution_authority` doctrine bans (broker_router / alpaca_credentials / kraken_credentials / may_execute / etc. cannot appear in either module's source).

### Live verification on preview
- `/api/admin/opinion-silence-watchdog/status` → 3 occupied seats, all returning real ages (1.4s / 188s / 207s).
- `/api/admin/opinion-silence-watchdog/scan?dry_run=true&threshold_sec=60` → correctly flags alpha + chevelle as stale, marks camaro as `skipped_fresh`.
- Background worker boots in lifespan: `opinion_silence_worker started: tick=900s threshold=14400s cooldown=1800s`.

### Tripwire status
595 tripwires green (up from 580+ baseline; +14 new tests, +1 sanity preserved). Other 76 non-tripwire HTTP-roundtrip failures (public-API, rate-limit, alpaca_execution_pipeline, etc.) are pre-existing and unrelated to this change.

### Doctrine pins reinforced
- ADVISORY OBSERVABILITY ONLY. Worker + route both source-scanned for forbidden execution imports.
- `perform_scan` is the sole detection path — operator-on-demand scan and autonomous worker scan cannot diverge.

### Next Action Items
- 🔴 P0 — Operator: redeploy preview → production (pushes pass #21 watchdog)
- 🔴 P0 — Operator: provision data-key env values on prod MC via Emergent Support
- 🟡 P0 — Brain authors (Alpha, Camaro): ship the `/api/ingest/opinion` patch per `RESPONSE_TO_ALPHA_AUTHOR_OPINIONS.md`
- 🟡 P1 — 6-Brain Expansion Refactor per `SIX_BRAIN_REFACTOR_PLAN.md`
- 🟡 P1 — Phase 2 Broker Bridge: real Kraken/Alpaca order placement in `shared/broker_router.py`

---


## 2026-05-28 (pass #20) — Opinion-silent watchdog + brain-author response docs

### Shipped
1. **`POST /api/admin/opinion-silence-watchdog/scan`** — scans every occupied seat, flags any holder whose last opinion is older than threshold (default 4h) or who has NEVER posted an opinion. Writes to `opinion_silence_alerts` collection with cooldown throttling (default 30min per brain/seat pair). Authority pin: `advisory_observability_only`.
2. **`GET /api/admin/opinion-silence-watchdog/recent`** — operator read for the last N alerts.
3. **`/app/memory/RESPONSE_TO_ALPHA_AUTHOR_OPINIONS.md`** — verified contract spec for Alpha's `POST /api/ingest/opinion` patch. Includes the 4 schema corrections (URL, header, collection name, stance vocab).
4. **`/app/memory/RESPONSE_TO_BRAIN_AUTHOR_ITER106z11.md`** — already on file from pass #19; redirects Camaro's broker-key proxy to the data-key endpoint.

### Live verification on preview
Dry-run scan with threshold=4h flagged exactly the seats Alpha-author predicted:
- `alpha @ strategist` — kind=never
- `chevelle @ governor` — kind=never

(Other seats vacant on preview; on production this would also flag any RedEye-occupied seat.)

### Why this watchdog matters
Pattern from Alpha-author's iter-106z11 follow-up: *"making it a logged event means it surfaces in alerts the moment a sidecar regression happens, instead of waiting for an operator to notice trades aren't firing."* The Seat Roster strip already shows opinion-silent visually; this endpoint makes it a **logged event** for downstream alerting (Slack/PagerDuty hookup, log analysis, audit forensics).

### Doctrine pin
The watchdog OBSERVES only. It NEVER:
- Forces a seat reassignment
- Vetoes an intent
- Modifies execution authority

All advisory observability. Operator-controlled.

### Next Action Items (unchanged from pass #19)
- 🔴 P0 — Operator: redeploy preview → production (pushes passes #15-20)
- 🔴 P0 — Operator: provision data-key env values on prod MC via Emergent Support
- 🟡 P0 — Alpha author: ship the `/api/ingest/opinion` patch per response.md
- 🟡 P0 — Other brain teams: copy Camaro's `mc_key_proxy.py` pattern (offered) and adopt it
- 🟡 P1 — Wire the watchdog as a periodic scan in FastAPI lifespan (currently manual via POST)
- 🟡 P1 — `/api/admin/intents/why-stuck/{intent_id}` diagnostic endpoint
- 🟢 P3 — Cleanup RedEye dead broker code (deferred)

### What this thread proved
Doctrine system worked under three distinct pressure tests this session:
1. **Broker-key proxy (Camaro author)** — proposed wrong endpoint, recognized violation, self-corrected, withdrew
2. **Sidecar opinions (Alpha author)** — diagnosed correctly but schema-wrong, accepted correction, will ship
3. **Operator pressure ("eliminate dry-run buttons")** — boundary held; doctrine pins explicit; no slippage

Three different actors, three correct outcomes, zero authority leaks.

---


## 2026-05-28 (pass #19, addendum) — Brain author feedback on broker keys

### iter-106z11 follow-up — RedEye author stood down on broker-key rip-out

RedEye's author independently reached the correct doctrinal conclusion after the response from `/app/memory/RESPONSE_TO_BRAIN_AUTHOR_ITER106z11.md` was sent. Key acknowledgments:

- RedEye's broker code (~983 LOC) is **inert legacy** — rotated Alpaca keys, blank IBKR env, `execute_trade` stub flagged in `wild_adaptive_core` notes
- Not a live doctrine violation (no active broker keys on the brain pod)
- Will be reclaimed as **P3 cleanup** once MC's `/api/admin/keys/market-data` endpoint is production-stable

### P3 cleanup task (deferred)
**When**: After MC data-key proxy has run stable on production for ~2 weeks AND orphan watchdog has confirmed zero broker-key writes from any brain pod across at least one full audit cycle

**What**: Drop the broker SDK + routes + env slots from RedEye (and apply same audit to Alpha/Camaro/Chevelle)

**Why wait**: Dead broker code is currently *evidence of compliance* (visible but inert). Premature deletion creates a window where a brain could be issued broker keys via misconfiguration without the orphan-fill detector catching it (because there's no SDK to fire orders with).

**Estimated LOC reclaim**: ~6% of RedEye backend (~983 lines). Same audit should be run against Alpha/Camaro/Chevelle to confirm similar dead-broker-code patterns across all four sidecars.

### Doctrine reinforcement
The brain author originally proposed `/api/admin/keys/broker` (would have re-opened 2026-05-23 orphan-execution path). After receiving the doctrine explanation, they:
- Recognized the violation
- Voluntarily withdrew the proposal
- Identified their own dead broker code as a cleanup target

This is the doctrine working correctly: load-bearing pins held; brain teams self-corrected when the boundary was made explicit.

---


## 2026-05-28 (pass #19) — Market-data key proxy + Seat-as-Authority labeling

### Two-part surgery, both doctrine-preserving

**Part A — Market-data key proxy** (`/api/admin/keys/market-data`)
Brain teams need their sidecars to read market data (bars, quotes, news, fundamentals) from third-party providers. When the 2026-05-23 audit revoked broker keys from sidecars, brains also lost their direct-to-Alpaca READ pipe (they were misusing broker keys for data too). The result: brains see stale/empty snapshots, fall back to HOLD with `STUCK_FEATURES_NO_DIVERSITY` veto.

Built MC endpoint to distribute DATA-source tokens (Polygon, Finnhub, Alpha Vantage, FRED, NewsAPI, SEC user-agent) to authenticated brain sidecars. **Broker keys remain impossible to leak through this surface by construction**:

- **Whitelist**: Only fields in `MARKET_DATA_KEY_FIELDS` are served
- **Forbidden fragments**: Any field name containing ALPACA / KRAKEN / IBKR / COINBASE / BINANCE / BROKER / SECRET_KEY / EXECUTE / TRADING_TOKEN / BROKER_TOKEN is rejected even if it makes it into the whitelist (defence in depth)
- Auth: same `<BRAIN>_INGEST_TOKEN` pattern as sidecar checkin (X-Brain-Id + X-Runtime-Token headers)
- Audit: every fetch logged to `market_data_key_fetches` collection
- Manifest endpoint (`/admin/keys/market-data/manifest`) publishes contract without values

**New backend files:**
- `routes/market_data_keys.py` — endpoint + auth + audit log
- `tests/test_market_data_keys_proxy.py` — **17 doctrine tripwires** locking the broker-key-leak-impossible invariant

**Part B — Seat-as-Authority labeling**
Operator decision: *"restrictions belong with the position not the brains. The seats restrict their movements."*

The Brain Console (`/admin/runtime/<brain>/console`) and Runtime Detail (`/admin/runtime/<brain>`) pages were showing the **promotion-ladder rank** (CHALLENGER / CO_TRADER / PRIMARY / ADVISOR) as if it were an authority concept. The backend had already collapsed ladder authority into seat policy on 2026-05-26 (`shared/routes.py:87-95` comment: *"authority_state field is kept for historical continuity but no longer gates anything"*) but the UI still implied a parallel restriction system.

Removed the parallel labeling. Both pages now show:
- **Top-right badge**: current seat (STRATEGIST / EXECUTOR / GOVERNOR / AUDITOR / CRYPTO_* variants) or **VACANT** if unseated
- **Brain Console Authority card**: "Seat" + "May execute" + "May veto" derived from seat policy
- Removed "Pending approvals" promotion-ladder approval flow from Brain Console
- Removed "LIVE EXEC: FALSE" misleading row (it was always a ladder-derived display gate; the seat already governs)

**Modified frontend files:**
- `pages/BrainConsole.jsx` — fetch roster, derive seat, replace ladder badge + State/Pending/Live exec rows, remove Pending approvals section
- `pages/RuntimeDetail.jsx` — fetch roster, derive seat, replace brain-name badge with seat-name badge

### Test summary
- 580 tripwires baseline (from pass #18); pass #19 adds 17 → **597 tripwires green**
- 1 pre-existing flaky test (`test_shelly_admin_endpoints_require_auth`) — passes in isolation; order-dependent

### To activate live ingest (still operator action on production)
1. Brain teams update their sidecars to call `GET /api/admin/keys/market-data` at boot with their existing `<BRAIN>_INGEST_TOKEN` header. Pull `POLYGON_API_KEY` / `FINNHUB_API_KEY` / etc. from the response into the sidecar's env.
2. Operator sets the actual key values in MC production env (Emergent Support env update):
   ```
   POLYGON_API_KEY=...
   FINNHUB_API_KEY=...
   ALPHA_VANTAGE_API_KEY=...
   FRED_API_KEY=...
   NEWSAPI_API_KEY=... (optional)
   ```
3. Restart MC + restart brain sidecars. Brains now have read-only data tokens. Brain-internal feature computation unblocks. `STUCK_FEATURES_NO_DIVERSITY` veto stops firing. BUY intents flow → MC gates green → trades fire through MC-owned broker keys.

### Brain teams' contract (paste in their docs)
```
GET https://mission.risedual.ai/api/admin/keys/market-data
Headers:
  X-Brain-Id: <camaro | alpha | chevelle | redeye>
  X-Runtime-Token: <same INGEST_TOKEN as /checkin>

Response 200:
{
  "brain": "...",
  "keys": {
    "POLYGON_API_KEY": "...",
    "FINNHUB_API_KEY": "...",
    "ALPHA_VANTAGE_API_KEY": "...",
    "FRED_API_KEY": "...",
    "SEC_EDGAR_USER_AGENT": "..."
  },
  "served_fields": [...],
  "unconfigured_fields": [...],
  "doctrine": "market_data_only",
  "ts": "..."
}

Optional probe (no auth): GET /api/admin/keys/market-data/manifest
```

### Doctrine pins added (D-DATA-KEYS-2026-05-28)
- MC may distribute DATA-source API keys to authenticated brain sidecars
- MC MUST NEVER distribute BROKER API keys (Alpaca, Kraken, IBKR, Coinbase, Binance)
- The boundary is enforced by whitelist + forbidden-fragments check
- Tripwire-pinned at 17 invariants

---


## 2026-05-27 (pass #16) — Opponent merged into Auditor + SeatRosterStrip live on Intents page

### Operator decision
With 4 brains (Alpha/Camaro/Chevelle/RedEye) and 5 seats per lane (= 10 seats across both lanes), the math didn't work — three seats were always empty. The empty seats made MC silently fall back to deterministic doctrine sidecars, producing identical-per-lane "strategist conviction · adversary objections · governor risk_mult" values across every intent (which on the screenshots looked like "MC rejecting every trade").

Doctrinal merge: **opponent absorbed into auditor**. The auditor seat now carries BOTH pre-trade contrary-case argument AND post-trade outcome review. Same brain, two time windows. Doctrinal rationale: both roles are skeptical/critical and sit OFF the execution path — combining them gives the brain that wrote the pre-mortem the natural seat to write the post-mortem.

### Resulting 4-seat doctrine (per lane)
| Seat | Doctrine |
|---|---|
| strategist | proposes thesis |
| governor | risk sizer |
| executor | fires intents |
| **auditor** | **contrary case (pre) · outcome review (post)** |

### Implementation pattern
Same `_LEGACY_ROLE_REWRITES` / `SEAT_ALIASES` alias-rewrite pattern as the earlier `decider → strategist` rename. Zero touches needed across the 25+ backend files + 5 frontend files that reference `opponent` strings — they continue to resolve via the alias table.

### Modified backend
- `shared/roster.py` — `opponent → auditor` and `crypto_opponent → crypto_auditor` added to `_LEGACY_ROLE_REWRITES`; `ROLES` tuple shrinks to 4 doctrinal seats per lane; `DEFAULT_ASSIGNMENTS` drops opponent keys
- `shared/seat_policy.py` — auditor absorbs opponent's `seat_required=True` and broadens `lane_scope` from `["equity"]` to `None`; new `crypto_auditor` entry; opponent row retained for legacy direct-readers but mirrors auditor permissions; `SEAT_ALIASES` updated

### Modified frontend
- `components/SeatRosterStrip.jsx` — shows 4 seats per lane with merged AUDITOR label (`contrary case · post-trade review`); grid columns 5 → 4; fixed timestamp rendering bug (was passing seconds-since-epoch to `relTime()` which expects ISO; replaced with local `formatAge(seconds)` helper)
- Pinned to `pages/Intents.jsx` right under PageHeader so all seats per lane are visible alongside the intent list

### Tripwires
- New: `tests/test_opponent_auditor_merge.py` — 15 tripwires locking the alias rewrites, permissions, lane scope, and the legacy-readers-still-work invariant
- Updated: `tests/test_paradox_namespace.py` — 2 stale tests that asserted on the old `advisor → opponent` alias now correctly point at `advisor → auditor` and `opponent → auditor`

### Test summary
- **564 tripwires pass, 0 fail** (up from 547)
- 15 new merge tripwires
- Backend hot-reloaded; no restart needed

### Why this fixes the "deadlocked rejection" symptom
Pre-merge: 3 empty equity seats + 5 empty crypto seats forced MC's gate chain to fall back on the deterministic doctrine sidecar for every brain voice. The sidecar packet produces identical-per-lane values from the snapshot's base labels, which the UI was displaying as if four independent brain voices had spoken. With 4 seats matching the 4 brains, all positions can be filled, the doctrine fallback is bypassed, and the gate chain sees real per-brain opinions per intent.

### Operator next step
Assign RedEye to the AUDITOR seat in both lanes via the existing `/admin/roster` panel. That brings the lane to 4/4 filled and removes the last source of doctrine fallback.

---


## 2026-05-27 (pass #15) — Shelly Phase 2: semantic retrieval via cloned local adapter

Operator-approved clone of `local_adapter.py` pattern into an embedding adapter, then wired Shelly as the first consumer. ADVISORY_ONLY throughout — no execution authority touched.

### New files
- `shared/llm/adapters/local_embedding_adapter.py` — fastembed BGE-small-en-v1.5 (384-dim, ~80MB ONNX, offline). Cloned shape from `local_adapter.py`. Lazy-loaded model; `is_ready()` checks dep presence only.
- `shared/llm/embed.py` — mini provider-dispatch kernel mirroring text-gen kernel: `embed_text`, `embed_texts`, `cosine_similarity`, `EMBED_DIM=384`. Future seam for `self_trained` + `openai` embedding adapters.
- `shelly/embeddings.py` — Shelly-side helpers: `memory_event_to_text` (deterministic serialization), `compute_event_embedding`, `cosine_rank` (pure-Python, no numpy on hot path).
- `tests/test_shelly_phase2_embeddings.py` — 16 tripwires.

### Modified
- `shelly/local_shelly.py` — `remember()` now computes + persists a 384-dim `embedding` field on each event (idempotent — same content → same vector). New `find_similar(case, top_k, min_score)` method does cosine retrieval over the brain's own memories.
- `shelly/routes.py` — new endpoint `POST /api/admin/shelly/find-similar` (operator-facing semantic retrieval probe).
- `requirements.txt` — added `fastembed==0.8.0` + `onnxruntime==1.26.0` (+51MB venv).

### Why this clone vs a Chroma sidecar
- Same SHADOW→PRIMARY doctrine as the text-gen kernel — future `self_trained_adapter` for embeddings is a drop-in.
- Mongo stays the truth store (vectors stored INSIDE the memory doc). No new infrastructure.
- fastembed (ONNX) is 10x smaller than torch+sentence-transformers; 51MB venv impact vs ~700MB.
- Phase 3 (Cross-Shelly federation) can later plug a vector index here without changing call sites.

### Doctrine pins (tripwire-locked)
- Every embed result carries `llm_authority="ADVISORY_ONLY"` (parity with text kernel).
- Embeddings inform retrieval; never modify execution authority, never gate intents, never modify RoadGuard.
- `memory_event_to_text` is deterministic (sorted feature keys; nested values skipped).
- `cosine_rank` tolerates Phase-1 memories without embeddings (silent skip, not crash).
- `find_similar` returns `[]` on empty pool rather than raising.

### Test summary
- **547 tripwires pass**, 1 unrelated pre-existing flaky test (test_lane_toggles_rejects_unknown_lane — passes in isolation, order-dependent issue in suite; NOT caused by Phase 2).
- 16 new Phase 2 tripwires; all green in isolation AND full-suite.

### Shadow self-training status (operator question, deferred to Phase 3+)
- LLM ledger (`llm_calls`) is accumulating ALL external LLM calls today — that's the corpus.
- `self_trained_adapter.py` is a stub — no actual model trained yet.
- `distillation_queue.py` referenced in `__init__.py` but not on disk.
- `eval_harness.py` uses Jaccard token overlap (its own TODO says "swap for embedding cosine once the embedding adapter exists" — that adapter now exists).
- Next time we revisit: build `distillation_queue.py` + shadow-mode parallel calls + swap eval_harness Jaccard → cosine.

---


## 2026-05-27 (pass #14) — Data Stack Phase 1 + tripwire suite back to 100% green

### Phase 1 Data Stack shipped
Operator-approved (DATA_STACK_PLAN.md Phase 1): Finnhub equity OHLCV (primary), SEC EDGAR Form-4 filings index, FRED macro series. Each runs as an async polling worker spawned in the FastAPI lifespan; each is a no-op until its `*_ENABLED=true` env-var is flipped. Missing API keys produce one row in `feeder_health_audit` and the worker idles.

### New backend modules
- `shared/feeders/feeder_health.py` — central rolling audit log helper (capped at 500 rows per provider)
- `shared/feeders/finnhub_equity.py` — OHLCV polling worker + weekly `/stock/profile2` refresh → `symbol_metadata`
- `shared/alt_data/sec_edgar.py` — Form-4 filings index poller; loads SEC's company_tickers.json once for CIK resolution
- `shared/alt_data/fred.py` — FRED macro series poller (CPIAUCNS, UNRATE, FEDFUNDS, DGS10, T10Y2Y by default)
- `routes/data_stack_admin.py` — operator endpoints (health audit, universe CRUD, symbol-metadata read, alt-data reads)

### New MongoDB collections
- `symbol_metadata` — float, market cap, sector, CIK per symbol
- `patterns_universe` — operator-managed watchlist (seeded with AAPL, MSFT, NVDA, TSLA, AMD, HOTH, AMC, GME)
- `feeder_health_audit` — per-feeder rolling 429/error log
- `alt_data_filings` — SEC EDGAR Form-4 index rows
- `alt_data_macro` — FRED series observations cache

### New API endpoints
- `GET /api/admin/feeders/health-audit`
- `GET/POST/DELETE /api/admin/patterns/universe[/{symbol}]`
- `GET /api/admin/symbol-metadata`
- `GET /api/admin/alt-data/filings`
- `GET /api/admin/alt-data/macro`

### Schema extensions
- `shared/technicals.py:FEEDERS` += `finnhub_equity` → `FINNHUB_FEEDER_TOKEN`
- `OHLCVBarIn.source` Literal extended to accept `finnhub_equity`
- Preferred-source order extended

### Doctrine pins (tripwire-locked)
- All three providers carry EVIDENCE only. No execution authority.
- `alt_data_macro` and `alt_data_filings` ingest paths strip `may_execute` defensively.
- All workers degrade gracefully on missing API keys → audit row + idle.
- Idempotent upserts everywhere (re-fetching same data = 0 net writes).

### Stale tripwires fixed (P1 from handoff)
- `test_intent_snapshot_persistence.py::test_admin_proxy_handles_missing_snapshot_as_empty_dict` — updated to assert sentinel `spread_bps=9999.0` + `spread_source="sentinel_unknown"` that auto-dry-run injects.
- `test_runtime_position_discovery.py` — `@pytest.fixture` → `@pytest_asyncio.fixture` for async-generator fixture; seed `updated_at` bumped to a far-future date so the seeded rows sort to the top of the limit=100 window.

### Test summary
- **532 tripwires pass, 0 fail** (up from 516 pass + 2 fail on handoff)
- 16 new Phase-1 tripwires in `tests/test_data_stack_phase1.py` (httpx MockTransport-based; no real network calls)

### .env additions (placeholders — operator fills keys to enable)
```
FINNHUB_API_KEY=
FINNHUB_FEEDER_TOKEN=
FINNHUB_ENABLED=false
FINNHUB_POLL_INTERVAL_SEC=300
FINNHUB_TIMEFRAME=5
FRED_API_KEY=
FRED_ENABLED=false
FRED_POLL_INTERVAL_SEC=86400
FRED_SERIES_IDS=CPIAUCNS,UNRATE,FEDFUNDS,DGS10,T10Y2Y
SEC_EDGAR_USER_AGENT=Risedual MissionControl ops@risedual.ai
SEC_EDGAR_ENABLED=false
SEC_EDGAR_POLL_INTERVAL_SEC=900
SEC_EDGAR_REQUEST_GAP_SEC=0.2
```

### To activate live ingest
1. Get FINNHUB_API_KEY at https://finnhub.io/dashboard (free; 60 calls/min)
2. Get FRED_API_KEY at https://fred.stlouisfed.org/docs/api/api_key.html (free; 120 req/min)
3. Set `FINNHUB_FEEDER_TOKEN` to a 32-hex token (matches what /api/ingest/ohlcv accepts)
4. Set `*_ENABLED=true` for the providers you want polling
5. `sudo supervisorctl restart backend`

---


## 2026-05-27 (pass #13) — 5-Shelly Memory/Reasoning Pipeline shipped

Operator-specified architecture built end-to-end: one LocalShelly per brain (4 today, N when `LIVE_RUNTIMES` expands), one MCShelly head, shared contract module, sync pymongo, fail-soft hooks, admin surface, 34 tripwires.

### Architecture
```
Alpha   → Shelly-Alpha    \
Camaro  → Shelly-Camaro    \
Chevelle→ Shelly-Chevelle   → MC Shelly → shared memory/reasoning
RedEye  → Shelly-RedEye    /

Brain Shelly  = local learning
MC Shelly     = shared memory head
MC core       = verifier / notary  (existing 12-gate chain)
RoadGuard     = safety              (existing market-structure guards)
Brains        = decision authority  (existing seat doctrine)
```

### Files shipped
- `shelly/contracts.py` — `ShellyMemoryEvent` + `ShellyReasoningReceipt` dataclasses. Locks vocabulary, confidence-delta bounds, authority tag. `event_hash` excludes `created_at` so idempotent upserts dedupe correctly (regression-guarded by tripwire).
- `shelly/local_shelly.py` — per-brain memory + reasoning. Idempotent `remember`, threshold-based `reason`, `rollup_for_mc` / `mark_rolled_to_mc` state machine.
- `shelly/mc_shelly.py` — head shelly. `ingest_rollup` dedupes by event_hash AND re-stamps authority at the boundary (tampered tags rejected). `reason_across_shellys` produces fleet verdict + brain-conflict detection.
- `shelly/pipeline.py` — `ShellyPipeline` singleton auto-extending with `LIVE_RUNTIMES`. Public hooks: `after_brain_receipt`, `nightly_shelly_rollup_job`.
- `shelly/sync_db.py` — sync pymongo client isolated from the motor async hot path.
- `shelly/routes.py` — admin endpoints: `GET /admin/shelly/status`, `POST /admin/shelly/rollup`, `POST /admin/shelly/reason`.
- `shelly/__init__.py` — public exports.

### Doctrine pins (locked by tripwires)
- **Allowed vocabulary**: `support` / `warn` / `neutral` / `seen_before` — ONLY.
- **Banned vocabulary**: `execute` / `block` / `override` / `promote` / `approve` / `reject` / `kill` / `force`. Every banned word has a parametrized tripwire that ensures `ShellyReasoningReceipt.to_doc()` raises on construction.
- **Authority tag**: every artifact carries `authority="memory_reasoning_only"`. Tampered tags rejected.
- **Confidence delta bounded** to `[-0.25, +0.10]` so Shelly cannot single-handedly tank or pump a brain's confidence.
- **Disjoint vocabularies**: allowed ∩ banned = ∅. Tested.
- **Auto-extends with LIVE_RUNTIMES**: when six-brain refactor lands, no Shelly file needs touching.

### Async vs sync decision
Initial implementation tried motor async; pytest's per-test event-loop binding produced "loop closed" errors on every DB call. User direction: keep it strictly sync. **Right architectural call** — Shelly intentionally runs outside the gate-chain critical path; a Shelly DB hiccup must not block live trading. Sync pymongo with a process-wide singleton client is the right shape. FastAPI auto-runs sync route handlers in the threadpool. From async paths, `asyncio.to_thread(after_brain_receipt, brain, receipt)`.

### Test summary
- 34 new tripwires in `tests/test_shelly_pipeline.py`. All pass.
- 514 total tripwires (up from 480, +34). Same 2 pre-existing unrelated failures.
- Lint clean across all new modules.
- End-to-end curl on preview: status endpoint returns canonical shape; reason probe returns neutral verdict with "0 shared cases" message; rollup endpoint idempotent.

### Coexistence with existing `shared/mc_shelly.py`
The legacy `mc_shelly` collection (generic event audit log) is UNTOUCHED. New collections are namespaced:
- `shelly_alpha_memories` / `shelly_alpha_reasoning_receipts` (× 4 brains)
- `shelly_mc_shared_memory` / `shelly_mc_reasoning_receipts`

A tripwire (`test_new_shelly_collections_distinct_from_existing_mc_shelly`) asserts disjointness so a future refactor can't merge them accidentally.

### Wire-in status (NOT yet active in production flow)
The `after_brain_receipt(brain, receipt)` hook is BUILT but not yet called from any existing code path. Wiring it in requires deciding WHERE in the intent/opinion/position ingest paths to attach. Recommended sites:
- `shared/intents.py:_ingest` — after `_fire_and_forget_dry_run`
- `shared/opinions.py:post_opinion` — after the opinion insert
- `shared/positions.py:post_position` — after position insert

Deferred to a future pass so the operator can review the integration surface separately.

### Operator next steps on PROD
1. Deploy pass #13.
2. Hit `GET /api/admin/shelly/status` — confirms all 4 LocalShellys initialized and the vocabulary is pinned.
3. Hit `POST /api/admin/shelly/reason` with `{symbol, direction}` to test the probe.
4. (Future) Decide where `after_brain_receipt` plugs into your existing brain emission paths.

---


## 2026-05-27 (pass #12) — SOV-AUDIT clarification + Pattern Watch tile + Sidecar Diagnostics aggregator

### Correction from pass #11 — the "21k mystery" is not a backlog

PROD screenshots revealed the actual schema: the prominent `21503` next to RedEye on the Diagnostics page is the **DECISION LOG** column, which counts rows in `sovereign_audit_log`, NOT pending intents in `shared_intents`. Source-cited from `shared/sovereign_mode_guard.py:385`: every accepted sovereign contribution writes one row to `sovereign_audit_log` per sidecar tick (~1/min). **21,503 rows ÷ 60s ≈ 358h ≈ 15 days of healthy operation.** These are heartbeat-style audit checkpoints, not stuck intents.

The auto-dry-run fix from pass #11 is still useful — it correctly addresses the `shared_intents.gate_state=pending` pile-up problem that DOES exist (verified on preview: 100 pending Camaro intents, drained successfully). The mistake was attributing the "21k" number to the same problem.

The actually-concerning signals from the PROD screenshots:
1. **CAMARO is DEAD** with 31,425s (8h+) stale heartbeat. Pod likely hung or OOM-killed.
2. **RedEye `LAST RECEIPT: —`** — zero gate-chain intent emissions despite 21k audit checkpoints. Either RedEye is intentionally audit-only (crypto_auditor role) or its signal-emit path is broken.

### #1 — Pattern Watch endpoint + Overview tile

`GET /api/admin/patterns/scan?limit=N&min_score=X&tf=X&breakout_only=bool&small_cap_only=bool` in `shared/technicals.py`:
- Ranks rows from `shared_pattern_snapshots` (populated by pass #10 detector) by `setup_score` descending.
- Returns `{filters, count, tier_counts, items, doctrine}`.
- `tier_counts` summary: `breakout_active`, `consolidation_only`, `uptrend_only`.
- Per-item operator-facing summary: symbol, tf, setup_score, ma200/consolidation/breakout booleans, breakout_pct + volume_surge_multiple, small_cap_qualified.

New `PatternWatchTile` on Overview:
- Heat-banded (green ≥1 breakout, amber ≥1 setup, gray otherwise).
- Top 8 symbols listed with per-row badges (BREAKOUT / CONSOLIDATING / SMALL CAP).
- Doctrine reminder rendered top-right: *"Descriptive evidence · brains decide"*.
- Fail-soft: if endpoint errors, tile silently omits (Overview page never blanks).

### #2 — Sidecar Diagnostics aggregator

New module `routes/sidecar_diagnostics.py`:
- `GET /api/admin/sidecar-diagnostics` — one curl returns every signal needed to triage "is each brain alive, contributing, emitting, discussing?"
- Pulls in parallel from `shared_heartbeats`, `sovereign_state`, `sovereign_audit_log`, `shared_intents`, `shared_brain_opinions`.
- Per-brain row: `{brain, verdict, operator_hint, heartbeat:{...}, sovereign_contribution:{live_count, audit_log_total, ...}, intents:{total, latest_*, ...}, opinions:{total, ...}}`.
- **`audit_log_total` is explicitly labeled** so no future reader confuses it with a backlog. This is the lesson from the 21k misread, pinned in schema.
- Verdict uses the SAME classifier as LivePulse (`connected` / `partial` / `stale` / `dead` / `never`) so panels never disagree.
- Per-brain `operator_hint` — one-line, actionable next step (e.g., *"Check sidecar pod logs — likely hung, OOM-killed, or rate-limited"* for dead brains).
- Fleet-wide rollup: `{total_brains, connected, partial, stale, dead, never, brains_with_no_intents_ever, brains_with_no_opinions_ever}`.

New `SidecarDiagnosticsTile` on Overview:
- Heat-banded by worst verdict in fleet.
- Header shows `X/Y connected · ATTENTION` band.
- Per-brain cards (4 in a 2-col grid): runtime label, verdict badge, operator hint, counter grid (intents / opinions / audit log / heartbeat age).
- Live verification on preview: Alpha=PARTIAL (heartbeat fresh but sovereign stale), Camaro=CONNECTED, Chevelle=PARTIAL with 0 intents ever, RedEye=STALE.

### Tripwires (12 new in `tests/test_pattern_watch_and_sidecar_diagnostics.py`)

Pattern Watch (6):
- Auth required
- Canonical response shape (top-level keys + `tier_counts` keys)
- Per-item schema keys pinned (so dashboard tile never silently breaks)
- `min_score` filter actually applies
- `breakout_only` filter actually filters
- Doctrine note mentions "evidence" + "never" + ("authority" OR "trigger")

Sidecar Diagnostics (6):
- Auth required
- Canonical top-level shape
- Fleet rollup keys pinned (8 expected counters)
- Per-brain shape pinned across all 5 sub-channels
- Verdict vocabulary locked to the LivePulse classifier set
- Doctrine note explains audit log is heartbeat, not backlog (so the 21k lesson is encoded forever)

### Test summary
- Tripwires: 480 pass (up from 468, +12). Same 2 pre-existing unrelated failures.
- Lint: clean across all modified files.
- Frontend: smoke-tested via screenshot — both new tiles render on Overview page.
- Endpoints verified end-to-end on preview.

### Operator next steps (PROD)
1. Hit `GET /api/admin/sidecar-diagnostics` on PROD. The output will show:
   - Whether CAMARO's 8h-stale heartbeat is recovered or still hung
   - Whether RedEye's intent emission path is actually broken or it's just an audit-only role
   - Which brains never emit intents (the `brains_with_no_intents_ever` counter)
2. The PROD `21k` number in DECISION LOG is healthy — leave it alone. If it grows past 60d worth of rows, the storage_rollup runner (pass #8) compacts it.
3. The Pattern Watch tile will populate as brains pull the technical feed. Currently sparse on preview (1 NVDA snapshot from earlier curl); will fill as brain-side consumers go online.

---


## 2026-05-27 (pass #11) — Auto-Dry-Run-on-Ingest + Backlog Drain (RedEye/Camaro "Not Moving" fix)

Operator diagnosed: RedEye showed "21k intents not moving" on PROD; preview confirmed Camaro had 100 PENDING intents accumulated, oldest 14 days old, never auto-evaluated. Root cause identified: **MC had no automatic dry-run worker**. Intents sat at `gate_state=pending` until an operator manually called `/execution/dry_run` for each one. This pass closes that gap.

### Diagnosis (full forensic on PROD + preview)

Three independent root causes uncovered:

1. **No auto-dry-run worker** (this fix) — Camaro's 100 PROD pending intents + preview's 6473 had no automatic evaluator. The "24 recognized vs 21k" pattern is exactly this: 24 got manually dry-run'd; 21k sat at pending.
2. **Vacant crypto seats on PREVIEW** (operator handles, not code) — preview had crypto/crypto_strategist/crypto_governor/crypto_auditor all `None`, so all RedEye crypto intents hard-blocked at `executor_seat_check`. PROD has crypto seats correctly assigned (Alpha exec, RedEye auditor) per operator screenshot.
3. **Sovereign contribution silent for 3/4 brains** (brain-side, not MC) — `contribution-health` confirms only Camaro hits `/sovereign/contribution`; Alpha/Chevelle/RedEye have `total_attempts: 0`. Source-cited last pass that this is what drives `HEARTBEAT ONLY` badges.

### #1 — Auto-Dry-Run-on-Ingest hook

`shared/intents.py:_fire_and_forget_dry_run`:
- Fires `_evaluate_gates` immediately after every `shared_intents.insert_one`.
- Wired into BOTH runtime-token ingest (line ~890) AND admin-proxy ingest (line ~1227).
- Fire-and-forget via `asyncio.create_task` so the brain's POST returns instantly (gate verdict lands ~50ms later).
- Failures swallowed — best-effort. If anything fails, the intent reverts to old behavior (stays at `pending`, operator can manually re-run).
- **Env-gated**: `AUTO_DRY_RUN_ON_INGEST` (default `true`). Operator flips to `false` on PROD for load relief while tuning; no code change needed.

### #2 — Reusable internal runner

`shared/execution.py:run_dry_run_for_intent(intent_id, order_notional_usd=10.0, actor=...)`:
- Extracted from `execution_dry_run` HTTP handler so both the auto hook and the new drain endpoint can share the exact same gate evaluation.
- HTTP handler is now a thin wrapper around this — zero behavior change for existing manual dry-run flows.

### #3 — One-Shot Drain endpoint

`POST /api/admin/intents/auto-dry-run-drain?limit=N&stack=...`:
- Catches up the backlog accumulated BEFORE this hook existed.
- Iterates all `gate_state=pending` intents, runs `run_dry_run_for_intent` on each.
- Idempotent: re-running after the first pass leaves zero pending rows.
- Per-intent failures logged but never halt the drain.
- Returns `{requested_limit, pending_found, processed, would_pass, would_block, failures, failure_count, doctrine_note}`.
- **Verified on preview**: drained 100 pending intents in one call → 100 would_block, 0 would_pass, 0 failures. Zero pending after.

### Tripwires (17 new in `tests/test_auto_dry_run_on_ingest.py`)
- Env gate: default ON; off via 5 falsy values; on via 5 truthy values
- `run_dry_run_for_intent` is importable + has the expected signature
- Drain endpoint requires auth + returns canonical schema
- Drain endpoint accepts `stack` filter
- **End-to-end regression guard**: post intent → wait → confirm `gate_state != pending`
- Disabled mode still works (env-gated escape hatch)
- Doctrine note pinned on drain response

### Test summary
- Tripwires: 468 pass (up from 451, +17). Same 2 pre-existing unrelated failures.
- Lint: clean across all modified files.
- End-to-end curl on preview: confirmed 100→0 drain.

### Operator next steps on PROD
1. Deploy this pass.
2. (Optional) Set `AUTO_DRY_RUN_ON_INGEST=true` explicitly in env. Default is already `true`.
3. Call `POST /api/admin/intents/auto-dry-run-drain?limit=500` to drain the existing PROD backlog. Repeat with higher limits if `pending_found` returns 500 (means more remain).
4. Future intent emissions auto-flip to `dry_run_passed` / `dry_run_blocked` within ~50ms. The PENDING column on the dashboard will drop to near-zero and stay there.

### Doctrine pin
Auto-dry-run does NOT grant execution authority. It ONLY transitions intents from `pending` → `dry_run_passed` / `dry_run_blocked`. Real execution still requires the operator to call `/execution/submit` with explicit `confirm=execute`. No behavior change to live trading; only visibility into the gate verdict was added.

---


## 2026-05-27 (pass #10) — Base-Formation Pattern Detector (Reddit setup)

Operator showed a Reddit chart: 3-signal small-cap pattern (long-term MA200 base → consolidation/volume accumulation → explosive breakout). Approved doctrinally-clean implementation: MC stamps evidence, brains judge evidence, seat holder acts. No gate, no authority, no hard blocks.

### Built (in order, per operator instruction)
1. **`shared/patterns/base_breakout.py`** — pure-function detector
   - Three deterministic signals from OHLCV bars (no DB, no env reads beyond module load):
     - `ma200_uptrend_active`: MA200 slope > 0 over trailing 30 bars
     - `consolidation_zone`: range ≤ 12% of MA200, ≥ 20 bars, MA(5/10/20/50) within 3% spread, with `volume_accumulation_score`
     - `explosive_breakout`: close > ceiling × 1.02, volume ≥ 1.8× 20-bar avg, fired within last 5 bars
   - Composite `setup_score ∈ [0, 1]` — weighted descriptive blend (MA200 0.30, Consolidation 0.40, Breakout 0.30)
   - `small_cap_qualified` flag — stamped IF caller provides `float_shares_millions` (default threshold ≤ 20M); `None` when unknown
   - Every threshold env-tunable via `PATTERN_*` env vars; `reload_env()` lets operator tighten mid-session
   - `config_snapshot` carried on every result for replay reproducibility
2. **Technical feed attachment** — `shared/technicals.py` 
   - Added optional `float_shares_millions` query param to both endpoints
   - `pattern_signals` attached to live + replay paths
   - Live path persists snapshot; replay path returns in-flight (no pollution)
3. **`shared_pattern_snapshots` collection** — new namespace in `namespaces.py`
   - Idempotent upsert keyed on `(source, symbol, tf, last_bar_ts)` — verified: 4 API calls = 1 row
   - Each row carries the full signals packet + `config_snapshot` + `computed_at` for Shelly training substrate

### Tripwires
- 18 pure-function tests in `tests/test_pattern_base_breakout.py`:
  - Schema contract (key sets, score range, ready flag)
  - Default thresholds pinned to operator-approved values
  - Insufficient-data paths return typed reasons (no exceptions)
  - Textbook pattern fires all three signals + score > 0.55
  - Volume-surge-insufficient → no breakout (false-breakout guard)
  - Close-below-ceiling → no breakout
  - Env-tunable: tightening consolidation range / breakout volume disqualifies
  - Small-cap qualifier: None / True / False paths
  - **Doctrine guard**: banned keys (`may_execute`, `execute_now`, `authority`, `requires_gate`, `force_buy`) MUST NOT appear in serialized payload
  - Composite score capped at 1.0
  - Config snapshot keys pinned

### End-to-end verified on real data
NVDA 1h (thinkorswim, 250 bars): `ma200_uptrend=True (slope +0.234/bar)`, `consolidation=True`, `breakout=False (no_breakout_in_window)`, `setup_score=0.58`, `small_cap_qualified=False` (NVDA float 2500M > 20M threshold). Snapshot persisted; re-calls hit upsert idempotently.

### What brains do now
Each sidecar's existing `/api/runtime-discussion/technical/{symbol}` pull now returns `pattern_signals` automatically. Brains decide how to weight `setup_score` in their own feature builders. **Not auto-promoted, not gated, not required.** Camaro might bias long; REDEYE might argue against late entries; Chevelle reads it as governance evidence. Their call.

### Test summary
- Tripwires: 451 pass (up from 433, +18). Same 2 pre-existing unrelated failures.
- Lint: clean across all modified files.
- Live API curl verified end-to-end (preview env).

---


## 2026-05-27 (pass #9) — Force-Close Removal + Stale-Conflict Alert + 3:1 R:R Gate

Operator delivered three fixes in one pass. P0 doctrine loophole closed, operator now sees conflict backlog at a glance, and equity entries face a deterministic 3:1 reward-to-risk floor.

### #1 — `broker_force_close_routes.py` DELETED (P0 doctrine)
- Removed `routes/broker_force_close_routes.py` entirely (315 lines, including `/admin/broker/force-close-all` and `/admin/broker/force-close-log`).
- Removed import + `include_router` lines from `server.py`.
- All position closes now MUST flow through MC's `CLOSE` intent verb → full 12-gate chain. No more operator override path that minted `OPERATOR_FORCED_CLOSE` receipts outside the gate evaluation.
- Tripwires: `test_broker_force_close_module_is_deleted`, `test_force_close_endpoint_returns_404`, `test_force_close_log_endpoint_returns_404` in `tests/test_force_close_removed_and_stale_conflicts.py`.

### #2 — Stale-Conflicts endpoint + Overview tile (P1)
- New endpoint `GET /api/admin/conflicts/stale?older_than_hours=24&limit=200` in `shared/conflicts.py`.
- Returns: `{count, oldest_age_hours, by_runtime, items, doctrine, generated_at}`.
- Only includes `status=open` conflicts past the threshold. `status=stale` (auto-resolved indecisive) is excluded.
- New `StaleConflictsTile` in `frontend/src/pages/Overview.jsx` — renders count + ACTION REQUIRED / ATTENTION / CLEAR band + per-runtime breakdown + triage queue link. Fail-soft (never blanks the page on backend error).
- Tripwires: 2 endpoint tests in `tests/test_force_close_removed_and_stale_conflicts.py` (schema + filter correctness).
- **Preview observation**: 200 open conflicts >24h, oldest 16.5d, distributed across alpha/redeye/camaro.

### #3 — Phase A R:R Gate at 3:1 (P1)
- New module `shared/rr_gate.py` with pure-function `evaluate_rr(intent)` returning `RRDecision`.
- Scope: equity lane + BUY/SHORT verbs ONLY. Crypto + exit verbs (SELL/COVER) pass cleanly with typed `RR_NOT_APPLICABLE_*` reasons.
- Math:
  - BUY: `reward = target - entry`; `risk = entry - stop`; ratio = reward/risk ≥ 3
  - SHORT: `reward = entry - target`; `risk = stop - entry`; ratio = reward/risk ≥ 3
- New optional fields `target_price` + `stop_price` on `IntentIn` (`shared/intents.py`). Persisted on both runtime-token + admin-proxy ingest paths.
- Gate inserted as `rr_ratio_floor` between `roadguard_spread_floor` and `governor_authority` in `shared/execution.py:_evaluate_gates`. `EXPECTED_GATES_IN_ORDER` updated in the diagnose contract.
- **Phase A is fail-SOFT** for intents missing `target_price` / `stop_price` (brain teams have a rollout window). Reason returned: `RR_MISSING_TARGET_OR_STOP`. Flip env `RR_REQUIRE_FIELDS_HARD=true` → Phase B hard-reject.
- **3:1 ratio enforcement is HARD from day one** — `RR_RATIO_BELOW_FLOOR` blocks. Floor is env-tunable via `RR_RATIO_MIN_EQUITY=3.0` (default).
- Direction-incoherent prices (target on wrong side of entry, etc.) → `RR_INVALID_PRICES` HARD REJECT in Phase A too.
- Tripwires: 18 tests in `tests/test_rr_gate.py` covering pass/fail at boundary, both directions, invalid prices, missing fields soft-pass, Phase B flip, crypto skip, exit-verb skip, env-tunable floor, and reason-vocabulary lock.
- **Curl-verified end-to-end**: 3:1 setup passes (`RR_RATIO_OK — reward/risk = 3.00 ≥ 3.0 floor`); 1.5:1 fails (`RR_RATIO_BELOW_FLOOR`); missing target/stop soft-passes.

### #4 — `HEARTBEAT ONLY` classifier diagnosis (no code change)
Operator asked: is the `partial`/`HEARTBEAT ONLY` badge gated on (a) last contribution received, or (b) whether the contribution carries *new* information (weights movement)?

**Answer (from source, `shared/heartbeat_ping.py:171-187`)**: AGE-BASED ONLY. The classifier checks `sovereign_state.updated_at < 300s`. There is NO weights-equality check, NO "defaults" gate, NO previous-tick comparison. So if a brain shows `HEARTBEAT ONLY · 22s ago`, MC is NOT seeing the sovereign contribution upsert at all — either the sidecar isn't calling `/api/runtime-discussion/sovereign/contribution`, or it's hitting 401/422 before `_persist_snapshot()` runs.

**Diagnostic curl**:
```bash
curl -s "$API_URL/api/admin/sovereign/contribution-health?window=200" -H "Authorization: Bearer $TOKEN"
```
Returns per-brain `{pushed_200, rejected_422, errors, top_empty_fields, latest_outcome}` — authoritative because logged from MC's side (same class as the runtime-token health endpoint shipped pass #8).

### Test summary
- Tripwires: 433 pass (up from 410, +23: 5 force-close/stale + 18 R:R). 2 pre-existing unrelated failures (`test_intent_snapshot_persistence` admin-proxy spread sentinel, `test_runtime_position_discovery` seeded fixture).
- Lint: clean across all modified files.

---


## 2026-05-27 (pass #8) — Doctrine Collapse + Liveness Truth

Operator ground truth: dashboard was lying. Camaro labeled DEAD while emitting 383 intents/24h. REDEYE had 21k backlog with only 24 recognized. Alpha 20× quieter than Camaro flagged as critical. Plus the REVIEW button only led to a splash page. Three fixes in one pass.

### #1 — Runtime-token rejection audit (REDEYE 21k mystery)

Found: REDEYE 401s never showed anywhere. The wrong-token POSTs just got dropped before persistence.

**New:** `shared/runtime_token_audit.py` — fire-and-forget audit writer hooked into `runtime_auth.verify_runtime_token`. Every 401/503 logs reason (`token_mismatch` / `missing_header` / `token_not_configured`) to `runtime_token_rejections` collection.

**New endpoint:** `GET /api/admin/runtime-tokens/health?window_hours=24` — returns per-brain rejection counts + diagnosis (`healthy` / `token_mismatch_high_volume` / `header_missing_high_volume`).

**Verified live:** sent a wrong-token POST → 401 surfaced as `token_mismatch` rejection row, picked up by health endpoint.

**Operator value:** when prod redeploys, REDEYE's misaligned token will surface within minutes as `token_mismatch_high_volume` on the health endpoint. Brain team can be pointed at hard evidence.

### #2 — Authority-ladder collapse (REVIEW splash-page dead end)

**Found:** the authority ladder (observer → advisor → challenger → co_trader → primary) was never actually gating execution in the auto-router or gate chain. It was purely a status badge in `shared/routes.py:90`. Two parallel gates (seat policy + authority state) existed; only seat policy mattered.

**Code:** `shared/routes.py:90` — `execution_allowed` now computed from seat occupancy + seat policy's `may_execute=True`, NOT from authority_state. Authority state remains as informational metadata on the response. `current_seat` field added so the UI can show which seat each brain occupies.

**Verified live:**
```
alpha    seat=executor   exec_allowed=True   ✅ (seat is gate)
camaro   seat=strategist exec_allowed=False  ✅ (correct doctrine)
chevelle seat=governor   exec_allowed=False  ✅ (governor never executes)
redeye   seat=None       exec_allowed=False  ✅
```

**Doctrine result:** drop Camaro into `crypto` seat → `exec_allowed=True` immediately. No promotion ladder, no REVIEW button, no splash page dead end.

### #3 — Multi-signal liveness (false-DEAD on Camaro)

**Found:** liveness was computed from sovereign-contribution age alone. Camaro had 383 intents/24h but stale sovereign → false DEAD.

**Code:** `routes/brain_emission_diagnose.py::_heartbeat_status` rewritten. Now reads four signals:
- `heartbeat_fresh` (< 2 min)
- `sovereign_fresh` (< 5 min)
- `intent_recent` (< 1 hour)
- `opinion_recent` (< 1 hour)

**Classification:**
- `active` = any of those + at least one productive signal (intent/opinion/sovereign)
- `dormant` = heartbeat fresh but otherwise quiet (Alpha's case — reachable but low conviction)
- `dead` = no signal of any kind

Also adds `intents_last_hour/24h` and `opinions_last_hour/24h` to the panel so the operator sees what each brain is actually doing.

**Verified live (preview):**
```
camaro   liveness=active   intents_24h=383
alpha    liveness=dormant  intents_24h=3
chevelle liveness=dormant  intents_24h=0
redeye   liveness=dead     no heartbeat row at all
```

### Tripwires (12 new, all passing)
- `tests/test_authority_collapse_and_token_audit.py` (7): overview exposes seat+authority separately, executor seat grants execution, governor never grants execution, seatless cannot execute, authority_state does not force execution, wrong token logs rejection, health endpoint lists all brains + diagnosis field.
- `tests/test_multi_signal_liveness.py` (5): liveness field present, multi-signal indicators present, intent/opinion counts exposed, any recent intent implies not-dead, sovereign-silent + intent-busy = active not dead.

### Operator next steps
1. **Redeploy prod** to push these three fixes
2. On the dashboard:
   - Camaro will flip from DEAD → ACTIVE
   - Alpha will read DORMANT (truthful — quiet but reachable)
   - REVIEW button + PENDING APPROVAL card no longer relevant (authority state is informational)
   - LIVE EXEC will compute from seat (no more all-FALSE)
3. **To enable Camaro trading:** `POST /api/admin/roster/assign {role:"crypto", brain:"camaro"}` → drops Camaro into crypto seat → `exec_allowed=True` → kill switch is then the only remaining gate.
4. **Find REDEYE token mismatch:** `GET /api/admin/runtime-tokens/health` will show the count and reason. Email the brain team with the hard number.

---


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
