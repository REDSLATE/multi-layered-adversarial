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
