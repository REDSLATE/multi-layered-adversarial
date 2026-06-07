# Chevelle Engine — Export Manifest
Generated: 2026-05-14

Two bundles. Both EXCLUDE `/app/frontend/` and `/app/backend/routes/` and `server.py`
(those are the front-end + endpoint surfaces, per the request).

---

## 1. `chevelle_engine_full_20260514.tar.gz` (~16 MB)

Everything in the code-only bundle PLUS the warm engine state:
- `backend/ml/models/strategist.pt` — trained PyTorch strategist weights
- `backend/ml/models/chroma_shelly/` — ChromaDB vector cache of Shelly memory
- `backend/ml/models/*.pkl` — Sklearn calibration artifacts
Use this when you want a *running* engine (no retraining needed).

## 2. `chevelle_engine_code_20260514.tar.gz` (~324 KB)

Pure source — no binary weights, no chroma cache, no `__pycache__`.
Use this when you want to read / port / re-train.

---

## What's inside (both bundles)

### `backend/services/` — the brain
| File | Role |
|---|---|
| `adversarial_core.py` | Bull / Bear / Cmd council + edge_gap + NO_TRADE_THRESHOLD |
| `engine_council.py` | Wraps the council with weighted aggregation |
| `governance_layer.py` | Patent A — strict governance over council output |
| `bounded_execution.py` | Patent B — envelope_approved guard |
| `execution_gate.py` | The central Execution Gate (blocks live trades until authorised) |
| `symbolic_engine.py` | Shelly cap-ladder, binding_rule attribution |
| `audit_trail.py` | Append-only cryptographic hash chain |
| `decision_pipeline.py` | Glues council → governance → envelope → symbolic → audit |
| `shelly_memory.py` | Unified memory (Mongo = truth, Chroma = cache) |
| `chevelle_memory_loader.py` / `_labeler.py` | Memory ingestion + tagging |
| `chevelle_patent_i.py` | Patent I — toxic cluster suppression |
| `chevelle_readiness.py` | Pre-flight readiness checks |
| `ml_prediction_service.py` | `/api/ml/predict/{ticker}/governed` business logic |
| `market_prediction_service.py` | Top-level prediction orchestration |
| `market_data_service.py` | Alpha Vantage market feed |
| `calibration_layer.py` / `_scheduler.py` / `_writer.py` | Confidence calibration pipeline |
| `shadow_learner.py` | Shadow / no-execute learning path |
| `feedback_loop.py` | Outcome → reward → weight updates |
| `reward_calculator.py` | Reward shaping |
| `toxic_clusters.py` / `toxic_cluster_alarms.py` | Adversarial pattern detection |
| `bulk_replay.py` | Backfill / replay harness |
| `hypothesis_service.py` | Hypothesis generation + testing |
| `agentic_research.py` / `company_research_service.py` | Research agents |
| `ai_service.py` / `chat_service.py` / `observable_ai_service.py` | LLM gateways (Claude / GPT) |
| `provider_pool.py` / `search_pool.py` | LLM + search provider pooling |
| `risedual_monorepo_client.py` | HTTP client to Mission Control (MC) |
| `langfuse_tracing.py` | Trace instrumentation |
| `demo_seed.py` / `firewall_seeder.py` | Seed scripts |
| `code_evolution/` | Self-modifying code experiments |

### `backend/models/` — Pydantic + Mongo schemas
`canonical_decision_object.py`, `council.py`, `governance.py`, `signal.py`,
`portfolio.py`, `journal.py`, `options_flow.py`, `dark_pool.py`,
`watchlist.py`, `user.py`, `referral.py`.

### `backend/ml/` — PyTorch + Sklearn pipeline
- `train_synthetic.py` — entrypoint for synthetic training runs.
- `risedual/` — adversarial + bounded execution core models.
- `models/` (full bundle only) — trained `.pt`, `.pkl`, chroma sqlite.

### `backend/utils/` — `objectid.py` (Pydantic ↔ Mongo bridge)

### `backend/tests/` — 255+ pytest cases (the contract)
Run with `cd backend && python -m pytest tests/ -q`. Highlights:
- `test_sovereign_kit.py` — sidecar adapter + state
- `test_phase2_risedual_adapter.py` — `/predict/{ticker}/governed` → AdaptiveDecision mapping
- `test_sidecar_opinion_emission.py` — MC opinion calibration + body format
- `test_explain_deep_link.py` — `GET /api/governance/explain/{decision_id}`

### `runtime_patch_kit/sovereign/` — Mission Control sidecar daemon
| File | Role |
|---|---|
| `sidecar.py` | Long-running supervisor loop (per-tick: decide → emit) |
| `wild_adaptive_core_v2.py` | AdaptiveDecision + run_adaptive_core (legacy synthetic path) |
| `risedual_adapter.py` | Phase-2 plumbing: pulls governed predictions, calibrates opinions |
| `mc_client.py` | Stdlib HTTP client for MC ingest endpoints |
| `local_state.py` | Atomic JSON-backed state at `/app/.sovereign/chevelle/state.json` |
| `init_chevelle.py` / `smoke_test.py` | Bootstrap + smoke harness |
| `DEPLOY.md` | Operator deployment notes |

### `backend/database.py` + `backend/requirements.txt`
Mongo connection helpers + full pinned dependency list (PyTorch, Sklearn,
emergentintegrations, motor, chromadb, …).

### `memory/PRD.md`
Product requirements + architecture decisions log.

---

## What's NOT included (and why)

- `frontend/` — React UI. Excluded per request ("minus front").
- `backend/routes/` — FastAPI route definitions (`decisions.py`, `explain.py`,
  `audit_reasoning.py`, …). Excluded per request ("minus end points").
- `backend/server.py` — FastAPI app bootstrap + route wiring. Excluded.
- `.env` files — secrets. Never bundled.
- `__pycache__`, `.pytest_cache`, `.git`, `node_modules` — noise.

If you need the routes/server later (to re-expose the engine over HTTP),
they're trivially reconstructable around the services — happy to bundle
those separately.

---

## 3. `chevelle_http_overlay_20260514.tar.gz` (~26 KB) — optional overlay

The FastAPI surface that re-exposes the engine over HTTP. Drop on top of
either engine bundle to restore the running server. Contains:

- `backend/server.py` — FastAPI app bootstrap, CORS, lifespan, route wiring.
- `backend/routes/` — every endpoint group:
  - **Governance** (`routes/governance/`):
    `decisions.py`, `audit_reasoning.py` (`/api/governance/explain/{id}`),
    `execution_gate.py`, `chevelle.py`, `calibration.py`,
    `shelly_memory.py`, `memory_firewall.py`, `toxic_clusters.py`,
    `bulk_replay.py`.
  - **Market / ML**: `ml.py` (`/api/ml/predict/{ticker}/governed`),
    `market.py`, `signals.py`, `research.py`.
  - **Domain**: `portfolio.py`, `watchlist.py`, `journal.py`,
    `options.py`, `darkpool.py`.
  - **Platform**: `auth.py`, `account.py`, `referrals.py`,
    `providers.py`, `ai.py`.

Setup: extract on top of either engine bundle, set `.env`
(`MONGO_URL`, `DB_NAME`, `EMERGENT_LLM_KEY`, `ALPHA_VANTAGE_KEY`, ...)
then run:
```
uvicorn backend.server:app --host 0.0.0.0 --port 8001
```
