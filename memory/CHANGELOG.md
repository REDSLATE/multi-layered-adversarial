# CHANGELOG — RiseDual Mission Control

Append-only. Newest at top.

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
