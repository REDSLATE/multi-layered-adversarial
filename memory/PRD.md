# RISEDUAL Mission Control — Monorepo PRD

## ⚠️ Cross-Session Repo Map (read first, agents)

The user operates **two distinct Git roots**, both named in the RISEDUAL family.
This `/app` is **only one of them**. Do not assume the other one's files exist
here.

| Tree | Role | Where |
|---|---|---|
| **REDEYE / runtime stack** *(this repo, `/app`)* | Mission Control monorepo: shared nervous system, FastAPI ingest, governed promotion, dashboard, runtime patch-kits | this Emergent session |
| **RISEDUALAI / Camaro side** *(other repo, NOT here)* | Full Camaro app: Governance Console UI, audit trail, REDEYE bridge HTTP wrapper, AI Core, Patents A–I | a different Emergent session |

### What lives only in the OTHER repo (do not look for them here)
- `/app/backend/services/redeye_short_bridge.py` *(consumer-side copy)*
- `/app/backend/services/redeye_features.py`
- `/app/backend/services/redeye_long_short_focus.py`
- `/app/backend/routes/research.py`
  - `POST /api/research/redeye/camaro-signal`
  - `POST /api/research/redeye/camaro-signal/from-market`
  - `_emit_camaro_audit()` — writes audit row, tolerates missing `alpha_alignment`
- `/app/backend/tests/test_redeye_short_bridge.py`
- `/app/backend/tests/test_redeye_long_short_focus.py`
- `/app/frontend/src/components/GovernancePanel.jsx`
  - `RedeyeCamaroFeedCard()` — last-10 viewer of audit rows
  - `RedeyePulseCard()` — live Pulse widget

### What this repo authoritatively owns
- The REDEYE → Camaro **contract** (`/app/runtime_patch_kit/redeye/PULSE_CONTRACT.md`)
- The bridge **producer** module (`/app/runtime_patch_kit/redeye/services/redeye_short_bridge.py`)
- CLI patch instructions (`/app/runtime_patch_kit/redeye/CLI_PATCH.md`)
- The `alpha_alignment` forward-compat field (validated REDEYE-side, tolerated RISEDUALAI-side)
- All 3 isolated-brain runtime patch-kits (Alpha / Camaro / Chevelle)
- **Code Evolution v0 patch-kit** (`/app/runtime_patch_kit/code_evolution/`)
  — paste-in folder for ALL FOUR stacks (Alpha/Camaro/Chevelle/REDEYE).
  Each stack hosts its own gate; each stack has its own audit trail.
  Doctrine: AI may audit, recommend tests, write receipts. AI may NOT
  run shell, promote code, or modify the gate. PROTECTED paths return
  HTTP 423 in-band; CRITICAL paths require dual-sign (mirrors Build 3).
  9/9 smoke tests pass, lint clean.
- **Cross-brain discussion layer** (`/app/backend/shared/opinions.py` +
  `/app/runtime_patch_kit/DISCUSSION_LAYER_PATCH.md`) — mediated through
  Mission Control, pull-only consumption, schema-enforced no-execution.
  Brains post opinions, read peers, and learn each other via the
  `/api/shared/roles-manifest` endpoint. None of the four brains can
  execute (paper or live) — `may_execute` is a closed field that schema-rejects
  any value other than `false`.
- **Role Scoring v0** (`/app/backend/shared/outcomes.py` +
  `/app/frontend/src/pages/Scorecards.jsx`) — Step 2 of the cross-brain
  training plan. Each brain gets a role-specific scorecard:
    * Alpha: "When am I good at longs?" — hit rate, Brier, calibration bands.
    * REDEYE: "When am I good at shorts?" — same + alpha_alignment breakdown.
    * Camaro: "When should I trust/reduce/veto/execute?" — per-stance metrics.
    * Chevelle: "Which outside signals are reliable?" — topic_breakdown.
  Operators (or Chevelle as the auditor) attach outcomes; brains may not
  resolve their own opinions. Scorecards are descriptive, never
  prescriptive — they don't gate promotions; Patent J + dual-sign still does.
  Runtime endpoint `/runtime-discussion/scorecard` is schema-scoped: a brain
  cannot read another brain's metrics via runtime auth.
- **Conflict Memory v0** (`/app/backend/shared/conflicts.py` +
  `/app/frontend/src/pages/Conflicts.jsx`) — Step 4 of the training plan.
  Auto-detection: when two brains post opposing stances on the same topic
  within 4h, the disagreement is flagged as a conflict. Idempotent on
  pair_ids. Auto-resolution from outcomes: when both participants are
  resolved, the conflict closes with the win-side as winner (or stale if
  neither won). Manual operator override path. Pair-scorecards show "X
  is right Y% of the times when contradicting Z" across all six pairs.
  **Pair temperature** — rolling 24h/7d/30d conflict counts surfaced as
  a heat band (cold/cool/warm/hot/blazing). Live data already reads
  ALPHA vs REDEYE = BLAZING with 11 decisive (45%/55%) — the dual-axis
  read separates skill from friction so the operator can tell where to
  focus learning vs where doctrine itself may need rethinking.
- **Regime + Source slicing (Steps 3 & 5)** (2026-02-09, after Conflict Memory)
  - `OpinionIn` gains optional top-level `regime` field (snake_case
    identifier, max 48 chars; `422` on garbage). Stored on each opinion
    and copied onto the outcome doc at resolve-time so aggregation is
    a single query.
  - Camaro scorecard (`runtime=camaro`) gains
    `regime_breakdown.{overall, endorse_only}` — answers "which stack
    do I trust under which regime?". Stance-stratified; endorse-only is
    the headline.
  - Chevelle scorecard (`runtime=chevelle`) gains `source_breakdown`
    keyed off each opinion's `evidence.source` (with `_unsourced`
    catch-all bucket). Sits alongside `topic_breakdown`.
  - Frontend (`Scorecards.jsx`) renders both panels with sortable
    hit-rate / wins / losses / n tables.
  - Patch-kit doc: `/app/runtime_patch_kit/REGIME_AND_SOURCE_TAGGING.md`
    explains the tagging contract for Camaro and Chevelle sidecars.
  - Tests: `/app/backend/tests/test_regime_and_source.py` (5/5 PASS).
- **Shared Technical Evidence Layer** (2026-02-09, replaces "per-brain charts")
  - **Doctrine**: OHLCV + indicators are shared evidence; same bars,
    four brains, four interpretations. No brain owns the feed.
  - **Write path**: `POST /api/ingest/ohlcv` (single) and
    `/api/ingest/ohlcv/batch` (up to 2000 bars) authenticated with
    `X-Feeder-Token`. Feeders configured today: `kraken_pro` (crypto),
    `thinkorswim` (other markets), `manual` (backfill). Each has its
    own env-var token so revocation is one line.
  - **Idempotency**: bars keyed on `(source, symbol, tf, ts)`. Re-ingest
    of same key updates the bar and recomputes the snapshot.
  - **Indicator engine**: pure-Python (`/app/backend/shared/indicators.py`)
    — SMA(20/50/200), EMA(12/26), RSI(14), MACD(12,26,9), BBands(20,2),
    ATR(14). Computed on ingest, stored as one snapshot per (source,
    symbol, tf); historical bars retained for replay.
  - **Read paths**:
    * `GET /api/shared/technical/symbols` — universe + last-bar times.
    * `GET /api/shared/technical/{symbol:path}?tf=&source=&bars=` —
      operator JWT, supports slashed symbols (`BTC/USD`).
    * `GET /api/runtime-discussion/technical/{symbol:path}?caller=&tf=`
      — runtime-token auth so brain sidecars can pull without an
      operator JWT. Same payload shape ⇒ replayable.
  - **Mission-page panel**: `TechnicalsPanel.jsx` embedded on Overview
    (no new route per operator directive). Shows the universe with
    source/symbol/tf rows; click to expand a snapshot card (Close, RSI,
    MACD hist, BB position, SMA20/50/200, ATR%). Polls every 20s.
  - **Feeder kit**: `/app/runtime_patch_kit/technicals/README.md`
    includes a complete Kraken Pro REST polling sidecar and a TOS shell
    + the `evidence.technical_ref` audit-replay handshake brains use
    when posting opinions that referenced the snapshot.
  - **Tests**: `/app/backend/tests/test_technicals.py` (20/20 PASS) —
    indicator math fixtures, idempotency, batch ingest, feeder-auth
    rejection paths, operator/runtime read shape, symbol 404.
  - Total backend pytest = **118/118**.
- **Feeder Slots strip** (2026-02-09, follow-up)
  - `GET /api/shared/technical/feeders` aggregates per-feeder status:
    last_bar_ts, symbol coverage, tf coverage, bar count, configured /
    awaiting / fresh / stale / live. tf-aware staleness (1h = 24h
    window, 1d = 48h window).
  - `FeedersStrip.jsx` Mission-page component — three slot cards
    (Kraken Pro headline, ThinkOrSwim, Manual). Click to expand setup
    details: endpoint URL, X-Feeder-Token env-var name, source field
    value, currently-feeding symbols/tfs, copy-to-clipboard helpers, and
    a pointer to the patch-kit doc.
  - **Login bug fix**: replaced the axios client in `lib/api.js` with a
    native-fetch shim (drop-in API surface — `api.get/post/put/delete`
    return `{data}`, errors expose `err.response`). axios 1.x's XHR
    adapter intermittently hung under the Cloudflare-fronted preview
    deploy. Also disabled PostHog session recording (it was wrapping
    fetch for replay).
- **Kraken Pro live connection** (2026-02-09)
  - **Encrypted credential storage**: `shared/credentials.py` — Fernet
    symmetric encryption with key in `CREDENTIALS_ENCRYPTION_KEY`
    (auto-generated and persisted to `backend/.env` on first run in
    local dev; required env-var in prod). API key + private key stored
    encrypted at rest; private key never returned by any endpoint.
  - **Kraken client**: `shared/kraken.py` — public OHLC fetch +
    HMAC-SHA512 signed private calls. Monotonic nonce persisted on the
    singleton doc, atomic max-bump on every call. Scope probe over
    Balance / OpenPositions / ClosedOrders / TradesHistory / Ledgers so
    UI can show which permissions the key was granted. Symbol mapping
    table for BTC/ETH/SOL/XRP/ADA/DOGE pairs.
  - **Endpoints** (`shared/kraken_routes.py`):
    * `POST /api/admin/kraken/connect` — probe-then-store-then-start.
      Refuses to persist keys if Balance probe denies.
    * `GET /api/admin/kraken/status` — connection summary (redacted).
    * `POST /api/admin/kraken/reprobe` — re-run scope probe.
    * `POST /api/admin/kraken/test` — cheap Balance call.
    * `POST /api/admin/kraken/poll` — force OHLC poll outside schedule.
    * `DELETE /api/admin/kraken/disconnect` — wipe creds + stop poller.
    * `POST /api/admin/kraken/execution` — flip the execution-allowed
      gate. Defaults False. Requires literal confirm phrase
      ("I authorize execution on Kraken" / "Disable execution"). Every
      flip is audit-logged.
    * `GET /api/admin/kraken/audit` — append-only action log.
  - **Auto-poller**: FastAPI lifespan task. Pulls configured pairs/tf
    every `poll_interval_seconds` (default 60s). Pushes bars through
    existing technicals ingest → snapshot recompute. Idempotent on bar
    key, so re-polled overlap doesn't dupe. Replaces the seeded
    synthetic BTC/ETH bars on first successful poll.
  - **Doctrine**: only read-scope endpoints are called by Mission
    Control. Trading endpoints (AddOrder/CancelOrder) are intentionally
    not wired. `execution_enabled` is a flag for the eventual wire-up;
    the brain layer's `may_execute` stays schema-pinned False.
  - **Frontend**: `KrakenConnect.jsx` — modal under the Kraken slot
    with paste-once API+private inputs, pair multiselect, tf picker,
    test-and-connect button. Connected view shows redacted previews,
    detected scopes (✓/✗), balance preview (top 3 assets), poller
    status, last-tick info, and the execution-toggle confirmation
    flow. Disconnect button wipes creds and stops the poller.
  - **Tests**: `/app/backend/tests/test_kraken.py` (17/17 PASS) —
    signing math against Kraken's documented test vector, Fernet
    round-trip, redact masking, all admin endpoints' auth + 404 +
    schema rejection paths, execution-toggle confirm-phrase guard,
    audit-log capture.
  - Total backend pytest = **135/135**.
- **Brain ↔ Technical Feed wiring (Option A)** (2026-02-09)
  - Backend: `GET /api/shared/technical/{symbol}` (and runtime variant)
    accept `as_of=<ISO 8601>`. When supplied, the indicator snapshot is
    recomputed from retained bars ≤ as_of using the same pure pipeline
    that builds live snapshots. Same response shape; `replayed: true`
    flag distinguishes audit replays from live reads.
  - Camaro patch kit (`PASTE_INTO_CAMARO_TECHNICALS.md`): explicit
    `read_technical → decide → post_opinion` pattern showing how Camaro
    pulls a snapshot, makes its judgement, and attaches
    `evidence.technical_ref` (source, symbol, tf, computed_at, indicators_used)
    plus `evidence.values` (the specific numbers it quoted) to the
    opinion. Note documents that other brains can paste the same
    pattern when they get sidecars later.
  - Frontend: `AuditReplay.jsx` component injected into the Discussion
    page. When any opinion carries `evidence.technical_ref`, the
    operator sees a "replay technical evidence" toggle. Click → fetches
    the historical snapshot via the new `as_of` path and renders an
    8-cell grid (Close, RSI, MACD hist, BB position, SMAs, ATR%) with
    quoted-vs-recomputed values side-by-side. Highlighted cells show
    the indicators Camaro explicitly cited in `evidence.values`.
  - Tests: `test_replay_at_past_timestamp`,
    `test_replay_404_when_no_bars_before_as_of`. Confirm strict
    historical correctness — live and replay returns at different
    timestamps give different values from the same DB state.
  - Total backend pytest = **137/137**.
- **Brain Roster — dynamic role assignment** (2026-02-09)
  - Four roles: `decider`, `executor`, `governor`, `advisor`. Four
    brains: alpha, camaro, chevelle, redeye. Operator assigns 1:1.
    Defaults match doctrine (camaro/alpha/chevelle/redeye in role order).
  - Backend: `/api/admin/roster` (GET) + `/assign` + `/swap` + `/reset`
    + `/audit`. Operator JWT auth. Singleton doc in MongoDB; audit log
    appends every change.
  - Doctrine guard: the roster is **descriptive metadata only**.
    `may_execute` remains schema-pinned False on every endpoint and
    every patch kit regardless of which brain holds "executor". The
    role labels record operator intent ("if execution were enabled,
    this brain would carry the orders") not authority.
  - Assignment behavior: putting a brain into a new role automatically
    vacates its previous role (no auto-fill — operator decides).
    `brain=None` explicitly vacates a role.
  - Opinion stamping: when any opinion is ingested, the brain's current
    role is captured into `posted_as` on the opinion doc. Best-effort
    (roster lookup failures don't block opinion writes). Lets the
    operator later see "Camaro endorsed *as decider*" vs.
    "Camaro endorsed *as advisor*" after a role change.
  - Frontend: `RosterPanel.jsx` on the Mission page (above the Feeder
    slots). Four role columns showing current occupant + role
    description; click "change" to swap in another brain; "vacate" to
    empty a role; "reset" restores defaults. Picker warns if a chosen
    brain currently holds a different role so the operator sees the
    cascade before committing.
  - Tests: `/app/backend/tests/test_roster.py` (19/19 PASS) — defaults,
    assign + auto-vacate, swap, swap-same-role rejection, bad role/brain
    422, reset, audit-log capture, auth required, opinion stamping with
    posted_as, posted_as reflects post-swap roster, **Eligibility matrix
    defaults + assign/swap enforcement + can't-disallow-current-occupant
    safety**, **Tenure KPI response shape + tenure resets on swap**.
  - **Role Tenure KPI** (`/api/admin/roster/tenure`): per-role
    `current_role_started_at`, `days_in_role`, `tenure_display`
    ("14d" / "3h"), `previous_role`. System-level:
    `total_swaps_90d`, `average_tenure_days`, `churn_state`
    (LOW ≤4 swaps · MEDIUM ≤12 · HIGH >12 in 90d), `last_swap`.
    Computed from the audit log (no new collection). Invariant
    documented in payload: tenure must never affect execution.
  - **Eligibility matrix** (`/api/admin/roster/eligibility`): operator
    switches deciding which seats each brain may occupy. Defaults
    encode training reality — chevelle = governor only, redeye =
    advisor only, alpha/camaro = decider/executor/advisor (not
    governor). `/assign` and `/swap` refuse to violate the matrix
    (400 with clear error). Disabling a switch is blocked while the
    brain currently holds that seat (vacate or swap first).
  - **Frontend** (`RosterPanel.jsx`): tenure shown inline per role
    ("in role: 14d") + churn badge in the header + footer KPI row
    (avg tenure, swaps 90d, last swap age, doctrine invariant).
    Eligibility switches toggle pane (collapsed by default) renders
    a 4×4 ALLOW/BLOCK matrix; ineligible brains are greyed out and
    marked "BLOCKED" in the role picker.
  - Total backend pytest = **156/156**.
- **IBKR Web API integration — Phase 1 (read-only)** (2026-02-11)
  - `shared/ibkr.py` — httpx OAuth 2.0 Bearer client against
    `api.ibkr.com`. Probe (`/iserver/auth/status` + `/iserver/accounts`),
    test, accounts, positions, tickle (single tick + 5-minute background
    loop), disconnect, execution-toggle gate, audit log.
  - Encrypted token storage uses the same Fernet path as Kraken
    (`shared/credentials.py`). The token is never returned past
    `redact()`; the encrypted blob lives in `ibkr_credentials`.
  - Endpoints (`/api/admin/ibkr/*`): connect, status, test, tickle,
    accounts, positions, disconnect, execution, audit. Every endpoint
    requires operator JWT.
  - Tickler: background asyncio task pings `/v1/api/tickle` every
    5 minutes so the IBKR session does not time out. Started on save,
    stopped on disconnect. Auto-revives on app boot if creds exist
    (lifespan hook in `server.py`).
  - Doctrine: trade endpoints (`/iserver/account/.../orders`, `/reply/*`)
    are **NOT** wired by this router. `execution_enabled` defaults False
    and is groundwork for the eventual Phase 2 dual-sign promotion.
  - Frontend: `IBKRConnect.jsx` modal under the new IBKR broker slot in
    `FeedersStrip.jsx` — paste access_token, optional account_id, base_url;
    test-and-connect; connected view shows auth status, tickler state,
    detected accounts, positions loader, exec-toggle confirm-phrase flow.
  - Tests: `/app/backend/tests/test_ibkr.py` (14/14 PASS) — disconnected
    status shape, schema rejection paths (short token, missing token,
    non-https base_url), 404s on every endpoint when unconfigured,
    disconnect idempotency, execution-toggle confirm-phrase guard
    against a seeded credential doc, audit log capture, JWT auth required
    on every admin path, `get_active()` returns None when nothing stored.
  - Total backend pytest = **170/170**.
- **Heat-map matrix — at-a-glance pair view** (2026-02-11)
  - Backend: `GET /api/shared/conflicts/matrix` aggregates ALL six
    brain-pair combinations into a single payload: skill (win rate,
    a_wins, b_wins, decisive), friction (temperature over 24h/7d/30d),
    and a 7d-derived heat band (cold/cool/warm/hot/blazing). One
    round-trip replaces N pair-scorecard fetches on the dashboard.
    Operator JWT required.
  - Frontend: `HeatMatrix` table on `Conflicts.jsx` above the existing
    pair scorecards — 4×4 grid where the row brain's win rate over the
    column brain is the headline number, the cell background hue is the
    7d friction colour, and the subline shows wins/decisive · 7d count.
    Diagonal shows `—`. Tooltip carries the raw counts.
  - Tests: `/app/backend/tests/test_conflict_matrix.py` (3/3 PASS) —
    response shape (6 cells for 4 brains, all required keys, no dupes),
    JWT required, matrix cell values cross-match the per-pair scorecard
    endpoint exactly (decisive, a_wins, b_wins, 7d friction, heat band).
  - Total backend pytest = **170/170**.
- **Doctrine loosening (2026-02-09)**: communication is unrestricted.
  Stance vocabulary expanded (added `agree`, `disagree`, `refine`,
  `retract`, `hypothesis`). Topic kinds permissive (any
  `[a-z_][a-z0-9_]*:value`, e.g. `regime:trend`, `theory:momentum_decay`).
  Evidence cap raised to 64 KB. Thread depth raised to 64.
  Trading remains hard-locked: `may_execute=true` schema-rejected at every
  layer; observation banner present.
- Mission Control backend, frontend dashboard, governed promotion (incl. dual-sign)

### Forward-compat rule between the two repos
1. **REDEYE always emits** every field defined in `PULSE_CONTRACT.md` (including `alpha_alignment`, default `null`).
2. **RISEDUALAI tolerates absence** — `_emit_camaro_audit` reads with `.get(...)` for any non-required field.
3. Schema additions are non-breaking when added as optional + null-default first.
4. Bump `contract_version` before any rename/repurpose.

---

## Original Problem Statement
Refactor three RISEDUAL projects (RISEDUAL-AI-2 → **Alpha**, RD4_0421 → **Camaro**,
2.1-APP → **Chevelle**) into one monorepo-style backend with **shared infrastructure** and
**isolated decision authority** per runtime. First deploy is OBSERVATION ONLY:
`BROKER_LIVE_ORDER_ENABLED=false`, `PHASE6_ENFORCE_ENABLED=false`,
`CAMARO_EXECUTOR_ENFORCE_ENABLED=false`, `CHEVELLE_AUTHORITY_ENABLED=false`.

Doctrine: **one shared nervous system, three separate decision brains.**

## Architecture (delivered)
- FastAPI backend (Python 3.11) in `/app/backend`
  - `server.py` — app factory, CORS, lifespan (indexes + seed)
  - `auth.py` — JWT (HS256) login/me/refresh/logout. Bearer header **and** cookie.
  - `db.py` — Motor MongoDB client + `ensure_indexes()`
  - `namespaces.py` — single source of truth for collection names
  - `shared/` — `routes.py`, `diagnostics.py`, `flags.py`, `seed.py`,
    `calibration_layer.py`, `memory_labeler.py`, `receipt_dispatch.py`,
    `feature_builders.py`, `artifact_inventory.py`
  - `runtimes/{alpha,camaro,chevelle}/routes.py` — runtime-isolated endpoints
- React frontend (CRA + Tailwind, dark terminal theme):
  - JWT auth via Bearer header (localStorage `risedual_access_token`)
  - Pages: Login, Overview, Receipts, Memory Firewall, Calibration,
    Feature Builders, Artifacts, Diagnostics, Flags, RuntimeDetail
- MongoDB collections (namespaced):
  - Shared: `shared_adl_receipts`, `shared_labeled_memories`, `shared_calibrators`,
    `shared_feature_builders`, `shared_artifact_inventory`
  - Per-runtime: `alpha_decision_log`, `camaro_shadow_rows`, `chevelle_memory_labels`

## What's Implemented (2026-01-09)
- Monorepo scaffold with shared/ + per-runtime/ split
- JWT auth (Bearer header) with seeded admin (`admin@risedual.io`)
- Brute-force lockout (5 fails / 15 min)
- Idempotent seed: 5 feature builders, 6 calibrators, 6 artifacts, 45 ADL receipts,
  36 memory labels, plus per-runtime decision logs
- Read-only flags endpoint (deploy_mode=observation, all enforce flags FALSE)
- Diagnostics endpoint (Mongo health + per-runtime liveness)
- Per-runtime endpoints isolated to their own collection (no cross-namespace reads)
- Unified admin dashboard: 3 color-coded runtime cards (Alpha blue / Camaro amber /
  Chevelle green), observation banner, doctrine card, flags strip, full ADL/memory/
  calibration/artifact/diagnostics views, runtime detail pages
- Backend tests: 38/38 PASS (iteration_1)
- Frontend tests: 16/16 PASS (iteration_2)

## What's Implemented (2026-02 — Visibility & Governance)
- **Build 5 — Heartbeat staleness alerts** (visibility-only, no broker side-effects)
- **Build 1 — Promotion Artifact emitter** in runtime patch-kits (Patent G evidence)
- **Build 4 — Recent Ingests live tail** page with polling
- **Build 3 — Dual-sign primary countersign** (2026-02-09)
  - Elevation TO `primary` requires two distinct operator signatures
  - First sign parks proposal in `awaiting_second_sign`
  - Same operator cannot occupy both slots (409 enforced server-side)
  - History records both signers; dashboard shows `n/m` signature progress
  - Patent J failure still blocks both signatures (gate cannot be bypassed)
  - Backend tests: 7/7 PASS (`tests/test_dual_sign_promotion.py`)
  - Existing single-sign rungs unchanged (back-compat verified)
- **REDEYE → Camaro short-side bridge patch-kit** (2026-02-09)
  - Path: `/app/runtime_patch_kit/redeye/`
  - Bridge module: `services/redeye_short_bridge.py` (pure stdlib)
  - Doctrine: REDEYE = short-side adversarial scout, reports to **Camaro only**,
    never Alpha. Camaro retains final execution authority.
  - `camaro_contract` block on every payload: `may_execute=False`,
    `may_override_alpha=False`, `final_authority=CAMARO`,
    `role=short_side_advisor`.
  - REDEYE not added as a 4th runtime in `namespaces.py` — it has no authority
    on the trading ladder by design.
  - Local smoke test (`smoke_test.py`) verifies SHORT/HOLD gates and the
    borrow-block override. PASS.
- **REDEYE Pulse contract — `alpha_alignment` forward-compat** (2026-02-09, A1)
  - New file: `/app/runtime_patch_kit/redeye/PULSE_CONTRACT.md`
  - Bridge gains optional `alpha_alignment` parameter (∈ `null|"aligned"|"divergent"|"contradicts"`)
  - Validation REDEYE-side: invalid value raises `ValueError` before payload leaves.
  - Default `null` always emitted so RISEDUALAI's `_emit_camaro_audit` always sees the field.
  - CLI patch updated: `--alpha-alignment` arg added.
  - Smoke test extended: default null, all 3 valid values round-trip, invalid raises. PASS.
  - Cross-session repo map added at top of this PRD so future forked agents don't
    confuse the two RISEDUAL repos.
- **Code Evolution v0 — per-stack AI gate for code patches** (2026-02-09)
  - New folder: `/app/runtime_patch_kit/code_evolution/`
  - Six service files (~960 LOC total): `schemas.py`, `ast_invariants.py`,
    `code_auditor.py`, `promotion_policy.py`, `receipts.py`, `api.py`,
    `deps.py` (the only stack-specific file).
  - Doctrine baked into source:
    * `may_auto_promote()` returns `False` under any args combination.
    * `PROTECTED_PATHS` blocks any in-band patch to the gate itself (HTTP 423).
    * No `subprocess` import in any file — AI cannot run shell.
  - Classification → action mapping: PROTECTED→423, CRITICAL→dual-sign,
    HIGH→single+24h cool-down, MEDIUM→single, LOW→single.
  - AST-based invariant scanner (not regex on diffs): catches forbidden
    constant assignments (`BROKER_LIVE_ORDER_ENABLED=True`,
    `risk_multiplier > 1.25`, etc.), forbidden calls (`paper_trades.insert*`,
    `drop_collection`, `delete_many`), syntax errors, and `target_files` vs
    `post_patch_files` drift.
  - Mongo persistence via `MotorDispatcher` (single collection
    `code_evolution_proposals`); `InMemoryDispatcher` provided for tests.
  - Endpoints: `POST /audit`, `GET /proposals`, `GET /proposals/{id}`,
    `POST /{id}/countersign`, `POST /{id}/reject`. All require auth.
  - Per-stack paste shells: `PASTE_INTO_{ALPHA,CAMARO,CHEVELLE,REDEYE}_AGENT.md`
    each tweak `EXECUTION_PATHS` and `FORBIDDEN_ASSIGNMENTS` for that stack.
  - 9/9 doctrine smoke tests pass; lint clean.
- **REDEYE dashboard page (admin-only)** (2026-02-09)
  - New file: `/app/frontend/src/pages/Redeye.jsx` mounted at `/redeye`.
  - Sidebar gets a new **"Advisors"** section, distinct from the
    "Runtimes" section, so REDEYE is visually marked as a Camaro-side
    advisor, not a peer brain. Red accent (`#DC2626`).
  - Page renders the doctrine corrected per operator: REDEYE advises
    Camaro; **neither executes**. Execution lives elsewhere on the
    ladder (Alpha + authority_state ∈ {co_trader, primary} + Patent J
    + operator countersign + observation-mode flag).
  - Sections: chain of authority, camaro_contract table (with the
    "final_authority=CAMARO is over advice, not a license to execute"
    clarification), alpha_alignment forward-compat semantics, frozen
    bridge thresholds, live-feed placeholder (pending Camaro forwarding
    endpoint), file references.
  - Admin-only access is automatic — every page except `/login` is
    behind the existing JWT-cookie + admin-seeded user gate.

## Core Requirements (static)
- Doctrine: shared infrastructure, isolated decision authority
- Observation-only first deploy (every enforce flag false)
- ADL receipts always `observed=true`, `executed=false` in observation mode
- Each runtime route reads only its namespaced collection

## Backlog / Next
**P1**
- **Build 2 — Demote / freeze workflow** (operator-initiated downgrade + hard-freeze
  endpoints, both audit-logged). On hold pending Build 3 production verification.
- TTL index on `login_attempts.ts` (currently unbounded — backend testing flagged
  as optional hardening).
- Refresh-token Bearer support: accept refresh token from JSON body / Authorization
  header (today only the cookie path is wired).
**P2**
- Real-time updates (websocket) for receipts + diagnostics.
- Drop-in slots for real Alpha/Camaro/Chevelle code (folder layout already mirrors
  the eventual import points).

## User Personas
- **Operator (Admin)** — single seeded role today. Reads dashboards, observes
  receipts, validates that all stacks remain in observation mode.

## Test Credentials
See `/app/memory/test_credentials.md`.
