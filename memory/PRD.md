# RISEDUAL Mission Control — Monorepo PRD

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
