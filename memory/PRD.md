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
