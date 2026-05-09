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

## Core Requirements (static)
- Doctrine: shared infrastructure, isolated decision authority
- Observation-only first deploy (every enforce flag false)
- ADL receipts always `observed=true`, `executed=false` in observation mode
- Each runtime route reads only its namespaced collection

## Backlog / Next
**P0**
- Promotion-gate workflow (operator-initiated, audit-logged) for flipping enforce
  flags out of observation mode (per-runtime, never collective).
**P1**
- TTL index on `login_attempts.ts` (currently unbounded — backend testing flagged
  as optional hardening).
- Refresh-token Bearer support: accept refresh token from JSON body / Authorization
  header (today only the cookie path is wired).
**P2**
- Receipt write API per runtime (currently the dispatcher exists but no inbound
  POST is exposed).
- Real-time updates (websocket) for receipts + diagnostics.
- Drop-in slots for real Alpha/Camaro/Chevelle code (folder layout already mirrors
  the eventual import points).

## User Personas
- **Operator (Admin)** — single seeded role today. Reads dashboards, observes
  receipts, validates that all stacks remain in observation mode.

## Test Credentials
See `/app/memory/test_credentials.md`.
