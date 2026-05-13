# RISEDUAL Mission Control — Monorepo PRD


## 🚀 Latest (2026-02-14): AI Investment Hypothesis Engine — Brain Recall
- `/admin/hypothesis` page: operator types ticker → dual brain-content card
- **Strategist** = brain in Executor seat. **Auditor** = brain in new rotatable Auditor seat.
- **NO external LLMs**. Pure recall over `shared_intents` + `shared_brain_opinions` + Shelly's `shared_labeled_memories` + `shared_brain_outcomes` (track record) + similar past setups via regime fingerprint
- 174ms typical query time. Client-side 30-min cache.
- Auditor seat seeded with REDEYE.

## Previously (2026-02-14): Alpaca Paper Broker Pipeline Live

- **Broker adapter** (`shared/broker/alpaca.py`) wraps `alpaca-py` SDK; paper-only hard-coded
- **Hard caps** ($10/order · $50/day · $100 open notional) enforced in code (`shared/exposure_caps.py`)
- **Full 8-gate chain** at `/api/execution/{dry_run, submit}` — schema · routability · executor seat · live-disable · broker connected · 3× exposure caps
- **Operator UI**: `AlpacaConnect.jsx` tile on `/admin/intents` (encrypted keys, status, ping). Per-intent `submit` button visible only when dry-run passes.
- Status: backend + frontend testing-agent verified. 24/24 unit + 10/10 integration tests pass.
- Awaiting user: paste Alpaca paper keys via the Connect Alpaca modal on `/admin/intents` to enable end-to-end paper execution.


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
- **Public.com retail brokerage — Phase 1 (read-only)** (2026-02-11)
  - **Why a third broker:** Public.com cash accounts have **no PDT
    restrictions** — when Phase 2 ships, this is the venue the executor
    brain can use for sub-$25k day-trade activity without IBKR's PDT
    gate or Kraken's crypto-only scope. Stocks, ETFs, options, and
    multi-leg strategies on the same key.
  - **Two-step auth** (per public.com/api/docs/quickstart):
    1. Operator generates a long-lived SECRET KEY at
       `public.com/settings/security/api`.
    2. We exchange the secret for a short-lived ACCESS TOKEN via
       `POST /userapiauthservice/personal/access-tokens` with
       `{validityInMinutes, secret}`. Default validity 24h, operator
       configurable 5 min … 7 d.
    3. Subsequent calls use the access_token as `Authorization: Bearer`.
  - **Encrypted storage**: secret + cached access_token both Fernet-encrypted
    via `shared/credentials.py` (same key path as Kraken/IBKR). Secret is
    never returned past `redact()`; plaintext token is never exposed.
  - **Background refresher**: asyncio task that polls every 60s and rolls
    the access token when it has ≤ 5 min remaining. Started on connect,
    stopped on disconnect. Auto-revives on app boot if creds exist.
  - **Endpoints** (`/api/admin/public/*`):
    * `POST /connect` — probe (token-exchange + account-discovery) then
      persist. Refuses to store if the secret can't exchange.
    * `GET /status` — redacted summary incl. token expiry, refresher state.
    * `POST /test` — calls `/userapigateway/trading/account`.
    * `POST /refresh-token` — operator-forced refresh.
    * `GET /accounts` — full account list.
    * `GET /portfolio` — positions + balances via
      `/userapigateway/trading/{accountId}/portfolio/v2`.
    * `DELETE /disconnect` — wipe secret + cached token + stop refresher.
    * `POST /execution` — flip the gate behind the same confirmation
      phrase ("I authorize execution on Public" / "Disable execution").
    * `GET /audit` — append-only action log.
  - **Doctrine**: Phase 1 is read-only. Order placement endpoints
    (`/userapigateway/trading/order/*`) are intentionally **NOT** wired;
    `execution_enabled` defaults False and is groundwork for Phase 2.
  - **Frontend**: `PublicConnect.jsx` modal under a new PUBLIC.COM
    broker slot in `FeedersStrip.jsx` (5 slots total now: Kraken / TOS
    / IBKR / Public / Manual). Operator pastes secret, optional
    account_id, base_url, token-validity-minutes; connected view shows
    token expiry countdown, refresher state, detected accounts,
    portfolio loader, exec-toggle confirm-phrase flow.
  - **Tests**: `/app/backend/tests/test_public.py` (15/15 PASS) —
    disconnected status shape, schema rejection paths (short secret,
    missing secret, non-https base_url, zero validity, excessive
    validity > 7 d), 404s on every endpoint when unconfigured,
    disconnect idempotency, execution-toggle confirm-phrase guard
    against a seeded credential doc, audit log capture, JWT auth
    required on every admin path.
  - Total backend pytest = **185/185**.
- **Seat policy is authority — identity is just training history** (2026-02-12)
  - **Doctrine codified**: `shared/seat_policy.py` declares per-seat
    permissions (`may_decide`, `may_execute`, `may_override`, `may_veto`,
    `speaks_as`) as a single source of truth. Every stance / decision /
    audit row snapshots the policy of the seat the brain held at write
    time, with `seat_epoch` to join back to roster history.
  - **Seat names cleaned**: `long_advisor` → `advisor` (neutral counsel),
    `short_advisor` → `opponent` (adversarial). 5 seats: decider,
    executor, governor, advisor, opponent. REDEYE → opponent. Advisor
    starts vacated. All eligibility + tenure + tests + frontend labels
    migrated.
  - **Per-position call mode** (`auto` | `manual`): operator chooses at
    propose-time. In `auto` mode, the first long/short stance from the
    brain holding the executor seat **immediately** advances state to
    `consensus_long`/`consensus_short` — drop any stack into Executor
    and it "just calls". Non-executor stances on auto positions DO NOT
    advance. `abstain` on auto positions does NOT advance.
  - **Per-(brain, seat) analytics** at `/api/admin/roster/seat-performance`:
    aggregates stances + executor calls + tenure-days for every
    (brain, seat) pair the brain has ever held. Answers "how good was
    Camaro as Executor?" with hard numbers instead of gut feel.
  - **`/api/admin/roster` payload** now includes the full `policy` dict
    and current `seat_epoch` so the frontend (and brain sidecars) can
    consult permissions without a second round-trip.
  - **Tests**: `/app/backend/tests/test_seat_policy_and_auto.py`
    (10/10 PASS) — policy exposed, snapshot fields on every stance,
    seat_epoch bumps on assign, auto-advance on executor long/short,
    no-advance on non-executor or executor-abstain, manual-mode never
    auto-advances, default call_mode is manual, seat-performance
    matrix returns expected rows, JWT auth required on analytics.
  - `tests/test_roster.py` + `tests/test_positions.py` migrated for
    the seat rename and policy snapshot fields.
  - Total backend pytest = **184/184**.
- **LivePulse honest-signal upgrade** (2026-02-13)
  - `/api/heartbeat-status/{brain}` now combines TWO signals so legacy
    `/api/ingest/heartbeat` background traffic stops false-greening the
    indicator. Verdict bands:
    * `connected` — heartbeat <90s AND sovereign contribution <300s.
    * `partial` — heartbeat present but no recent sovereign
      contribution (most common confusion mode: legacy ingest only or
      sidecar crashed mid-tick).
    * `stale` — last sovereign contribution 5-30 min ago.
    * `dead` — neither signal recent.
    * `never` — neither signal has ever been seen for this brain.
  - Response carries `heartbeat_age_seconds` + `contribution_age_seconds`
    so the operator can hover the LivePulse tooltip and see WHY the
    state is what it is.
  - LivePulse renders `partial` as amber "HEARTBEAT ONLY" with hover-
    text breakdown.
  - **Real connection census** (current state from first deployment
    wave):
    * Alpha: `connected` (real sidecar — contribution every 60s)
    * Chevelle: `connected` (real sidecar)
    * Camaro: `partial` (sovereign sidecar pending; discussion-layer
      opinions live)
    * REDEYE: `stale` (contributed earlier in session, last seen ~17m)
  - Tests `tests/test_heartbeat_status.py` (4/4 PASS) updated for the
    combined-signal contract.
- **LivePulse connection indicator on /runtime/{brain}** (2026-02-13)
  - **Backend**: new read-only `GET /api/heartbeat-status/{brain}`
    endpoint (no auth — same exposure as the existing public /ping
    pages). Returns `connected` band (`never` / `fresh` / `stale` /
    `dead`), `last_seen` ISO timestamp, and `age_seconds`. Banding:
    fresh < 90s, stale < 10min, dead beyond.
  - **Frontend**: `LivePulse` component polls `/heartbeat-status/{brain}`
    every 5s, renders a pulsing dot in the page header next to the
    brain badge. Green pulse when fresh (connected · 21s ago), amber
    static for stale, red for dead, grey for never. The pulse uses
    `animate-ping` so a brain coming online is impossible to miss
    visually.
  - **Tests** `tests/test_heartbeat_status.py` (4/4 PASS): unknown
    brain → 404, never-pinged state, fresh-after-ping state, no JWT
    required.
  - **Heartbeats collection reset** so the dashboard shows the honest
    "no heartbeat yet" state until a real brain host connects.
- **Sovereign onboarding packets + DEPLOY runbook** (2026-02-13)
  - **Smoke-test cleanup**: dropped 4 sovereign_state rows + 70 history
    rows + 70 audit rows + chat / narrative / traffic / rate-limit
    collections so the operator console shows the honest empty state
    ("No sovereign snapshot on file") until real brains connect.
  - **`DEPLOY.md`** at `/app/runtime_patch_kit/sovereign/` —
    5-minute deploy recipe (clone kit → set env → smoke test → run
    sidecar), systemd unit example, Dockerfile example, troubleshooting
    matrix, mode-switching notes (DTD↔PRD), broker-feed wiring path.
  - **Per-brain onboarding packets**: one self-contained markdown file
    per brain with the exact ingest token, suggested initial weights
    (creating distinct personalities), suggested symbol list, and
    copy/paste quickstart:
    * `ONBOARDING_ALPHA.md` — trend follower (trend +0.85, macd +0.65,
      rsi −0.25), lr 0.06, default seat Decider.
    * `ONBOARDING_CAMARO.md` — mean reverter (trend −0.45, macd +0.20,
      rsi +0.80), lr 0.05, default seat Advisor/Opponent.
    * `ONBOARDING_CHEVELLE.md` — risk auditor / governor (balanced 0.35
      across features), lr 0.02 (slow, deliberate), default seat
      Governor (holds the veto bit).
    * `ONBOARDING_REDEYE.md` — contrarian (trend −0.70, macd −0.30,
      rsi +0.55), lr 0.05, default seat Opponent.
  - **Doctrine reminder in every packet**: `LIVE_TRADING_ENABLED=False`
    is non-negotiable; brains write only to local state and via the
    three MC HTTP endpoints; PRD mode disallows training.
  - **Current state**: zero brain hosts connected. Architecture ready
    end-to-end; the deploy is a 5-minute per-brain task whenever the
    operator decides to run it.
- **Per-tier rate limits on /api/public/*** (2026-02-13)
  - **Defaults (per minute)**: free 30 · starter 60 · pro 300 · pro_max 1200.
    `unknown` tier (caller misspelled the header) caps at 20 as a
    belt-and-suspenders defense. Each tier's limit overrideable via env:
    `RATE_LIMIT_{FREE,STARTER,PRO,PRO_MAX}_PER_MIN`.
  - **Mechanism**: per-minute bucket counter in
    `public_rate_limits` collection, atomic `$inc` via
    `find_one_and_update(upsert=True, return_document=True)`. TTL index
    on `expire_at_epoch` drops buckets after 2 minutes — collection
    stays tiny regardless of traffic. Fails OPEN on Mongo hiccups —
    the rate limiter must never become a 5xx source for callers.
  - **Behavior**:
    * 200 responses carry `X-RateLimit-Tier`, `-Limit`, `-Remaining`,
      `-Window` so risedual.ai's backend can surface remaining quota.
    * 429 responses carry the same headers plus `Retry-After` (seconds
      until the next bucket).
    * Missing `X-RiseDual-Token` skips the rate-limit increment so
      random scrapers can't lock out legit callers (the trust dep
      still 401s).
    * Unknown tier values normalize to `unknown` (sentinel) so arbitrary
      strings don't pollute the bucket key space.
  - **Middleware ordering (important)**: `rate_limit_middleware` is
    inner, `public_traffic_middleware` is outer — so 429s emitted by
    the rate limiter are still seen + logged by the traffic logger.
  - **Admin endpoint** `GET /api/admin/public-traffic/limits` returns
    the current cap table — surfaced as a "Tier Rate Limits" tile on
    the `/public-traffic` operator page.
  - **Tests** `tests/test_public_rate_limit.py` (8/8 PASS, ~3.5min
    because the tests wait for minute-bucket rollover):
    * `/limits` endpoint requires JWT and returns the cap table.
    * 200 responses carry the X-RateLimit-* headers (verified for pro_max).
    * Free-tier 30/min cap: 35 calls → exactly 30×200 + 5×429.
    * 429 carries `Retry-After`, `X-RateLimit-Tier=free`,
      `X-RateLimit-Limit=30`, `X-RateLimit-Remaining=0`.
    * Pro Max immune to free-tier cap (50 consecutive calls all 200).
    * Missing trust token: not rate-limited, but still 401 (auth dep
      handles it).
    * 429 rows appear in the public-traffic log with `status=429` and
      the proper `tier` value — operator can filter for them.
- **Public Traffic verification page** (2026-02-13)
  - **Backend middleware** `public_traffic_middleware` mounted globally:
    captures every `/api/public/*` request — path, method, query,
    status, latency_ms, tier header, caller_ip. Fire-and-forget log
    insert; never blocks the live request even if Mongo hiccups.
  - **Admin endpoints** (`/api/admin/public-traffic/*`, JWT-gated):
    * `GET /admin/public-traffic` — last N rows, filterable by path /
      status / tier.
    * `GET /admin/public-traffic/summary?hours=N` — total + by-endpoint
      / by-tier / by-status counts + p50/p95/p99 latency.
    * `DELETE /admin/public-traffic` — clear all rows (manual reset).
  - **Frontend** `/public-traffic` page (operator-only, in nav):
    summary tiles (Total, Latency p50/p95/p99, By Tier, By Status),
    By-Endpoint horizontal bar chart, live tail table with status +
    tier color-coding, filters (window 1h-7d, path contains, status,
    tier), auto-refresh every 5s, clear-log button.
  - Smoke-tested live: 12 mixed requests across free/starter/pro/pro_max
    + 401s render correctly with proper coloring and aggregation.
- **Public API Phase 2 — LLM features + dual-token rotation** (2026-02-13)
  - **Integration**: Emergent LLM key (universal). Two models, picked
    for cost/quality fit:
    * `gemini:gemini-3-flash-preview` — narrative summary (cheap broadcast).
    * `anthropic:claude-sonnet-4-5-20250929` — grounded chat (deep reasoning, lower volume).
  - **`GET /api/public/digest/narrative`** — 3-5 sentence prose
    overview of today's market posture. System prompt anchors the
    model on the supplied JSON (predictions/smart_money/alerts), forbids
    fabricating numbers, no markdown / disclaimers. Cached server-side
    by 5-minute time bucket so dashboard refreshes don't burn tokens.
    Available to all tiers (content is not gated — same market).
  - **`POST /api/public/chat`** — multi-turn grounded RiseDualGPT.
    Pro Max only (returns 403 otherwise). Session memory persisted
    to `public_chat_messages` collection keyed by `session_id`;
    survives MC restarts. Prior conversation replayed into the LLM
    via injected "prior conversation" block on each turn (bounded by
    `MAX_TURNS_PER_SESSION=25`). System prompt enforces
    observation-only doctrine: model explains what signals say, will
    NOT recommend buy/sell.
  - **`GET /api/public/chat/history/{session_id}`** — repaint chat
    panel after a reload.
  - **`DELETE /api/public/chat/history/{session_id}`** — clear
    session memory (end of conversation).
  - **Dual-token rotation grace mode**: `auth.public_trust_required`
    now accepts EITHER `RISEDUAL_PUBLIC_TOKEN` (primary) OR
    `RISEDUAL_PUBLIC_TOKEN_OLD` (legacy). Operator rolls MC's env var
    independently of risedual.ai's deploy schedule — no broken
    interval. Documented in
    `runtime_patch_kit/risedual_public/ENV_CHECKLIST.md`.
  - **Paste-in kit updated**:
    * `types.ts` — adds `NarrativeResponse`, `ChatRequest`,
      `ChatResponse`, `ChatMessage`, `ChatHistoryResponse`.
    * `mcPublicClient.ts` — `digestNarrative()`, `chat()`,
      `chatHistory()`, `chatClear()`.
    * `SWAP_NOTES.md` — Phase 2 swap section (narrative + chat).
    * `ENV_CHECKLIST.md` — dual-token rotation procedure documented.
  - **Tests**: `/app/backend/tests/test_public_phase2.py` (14/15 PASS,
    1 skipped intentionally — long-running variant covered by
    multi-turn test):
    * Narrative returns grounded prose, model = gemini, all tiers OK.
    * Narrative second call hits cache (text identical).
    * Chat returns 403 for free / starter / pro.
    * Chat continues a session (same session_id, turn_count increments).
    * Chat history GET / DELETE work; 403 for non-pro_max.
    * Input validation (empty message → 422).
    * Dual-token: in-process tests verify both tokens accepted when
      legacy is set, primary-only when not, third value refused.
  - **Total backend pytest = 62/63 + sovereign passing** (Phase 2:14
    + Phase 1:26 + Sovereign:22 = 62/63 with 1 skipped; full backend
    suite carries forward all previously passing tests).
- **Public API for risedual.ai (Direction C, Phase 1)** (2026-02-13)
  - **Doctrine codified**: Two faces, one brain. risedual.ai keeps its
    Stripe + credits + 4-tier auth + UI; MC becomes the silent
    intelligence backend. Trust contract = `X-RiseDual-Token` (shared
    secret) + `X-RiseDual-User-Tier` (free/starter/pro/pro_max
    propagated from risedual.ai's user model; free and starter both
    treated as non-paid; only pro/pro_max get uncapped digest rows).
  - **MC env var**: `RISEDUAL_PUBLIC_TOKEN`. Missing → 503; wrong → 401;
    unknown tier → 422.
  - **Endpoints** at `/api/public/*`, all read-only, all sanitized:
    * `GET /signals` — Active Signals + aggregate AI Consensus
      (BULLISH/BEARISH/NEUTRAL/MIXED + buy/sell/hold percentages).
    * `GET /signals/{id}` — both framings of the same position:
      adversarial (Bull/Bear/Commander ↔ decider/opponent/executor
      seats) AND governance (Strategist/Auditor/Synthesized Signal ↔
      decider/governor/executor). Hides memory provenance, quorum
      blindness, seat_epoch.
    * `GET /digest` — predictions / smart_money / alerts with caps
      `{2/2/1}` for free+starter (+ locked-CTA rows) and `{25/25/25}`
      for pro/pro_max. Shapes match risedual.ai's existing
      `collect_digest_data` exactly.
    * `GET /scanner/presets` + `/scan?preset_id=…` — 10 presets
      (macd_bullish_cross, macd_bearish_cross, bollinger_squeeze,
      ema_golden_cross, volume_spike, near_52w_high, near_52w_low,
      rsi_overbought, rsi_oversold, momentum_breakout). Detection
      logic uses MC's stored indicator snapshots + recent OHLCV.
      Match shape `{symbol, strength, detail}`.
    * `GET /agent-activity/feed?since=&limit=` — polled feed
      synthesized from position audit + conflicts + outcomes.
      ~10s cadence on the client.
    * `GET /models-mind/{symbol}` — 10-feature panel
      (score_2W, distance_from_mw, macro_regime_flag, atr_id,
      earnings_proximity, momentum_3d, sector_rs, pattern_score,
      rsi_id, vol_zscore). MC defines these canonically (names didn't
      exist in risedual.ai's actual backend); computed from real
      technicals; `coverage: "not_wired"` for features MC can't yet
      compute (earnings_proximity, sector_rs).
    * `GET /heatmap` — per-symbol 24h % change + color band
      (strong_buy / mild_buy / neutral / mild_sell / strong_sell).
    * `GET /sectors` — XLK/XLF/XLV/XLY/XLP/XLE/XLI/XLU/XLB/XLRE/XLC
      universe. `degraded: true` until sector ETFs are wired into a
      feeder.
  - **Module split**: `/app/backend/shared/public_api/` with one file
    per endpoint group (`auth.py`, `signals.py`, `digest.py`,
    `scanner.py`, `agent_activity.py`, `models_mind.py`, `heatmap.py`,
    `router.py`).
  - **Paste-in kit** at `/app/runtime_patch_kit/risedual_public/`:
    * `README.md` — architecture, trust contract, rollout plan.
    * `types.ts` — exhaustive TypeScript types for every endpoint.
    * `mcPublicClient.ts` — drop-in Node/Next backend client.
    * `python_types.py` — Pydantic v2 mirrors for backend re-validation.
    * `SWAP_NOTES.md` — per-page mapping for risedual.ai's frontend.
    * `ENV_CHECKLIST.md` — env vars + rotation procedure.
  - **What MC does NOT do**: no Stripe, no credit ledger, no user
    accounts, no PCI scope. risedual.ai keeps all of it. MC's tier
    header only governs content sanitization (locked rows), not
    feature gating (risedual.ai's existing tier checks gate that).
  - **Tests**: `/app/backend/tests/test_public_api.py` (26/26 PASS) —
    trust auth (missing/wrong/unknown-tier), default-free tier,
    starter is unpaid, pro_max unlimited; signal card shape, both
    framings, 404; digest free/starter caps, pro/pro_max uncapped,
    locked-row shape; scanner 10 presets, match shape, unknown-preset
    404; agent-activity shape + since filter; models-mind 10-feature
    shape + not-wired markers + 404; heatmap + sectors universe.
  - **Total backend pytest = 243/243** (195 prior + 22 sovereign + 26 public).
- **Sovereign Sidecar Template** (2026-02-13)
  - **Doctrine**: each of the four brains can run as a deterministic
    sovereign sidecar — same intelligence core
    (`wild_adaptive_core_v2.py`), different initializations / feature
    emphasis per brain. Local state on the brain host (JSON), MC
    receives stances + state snapshots via API only. Never touches MC's
    DB directly.
  - **Three locks, one door** (observation-only):
    1. Brain core defaults `LIVE_TRADING_ENABLED = False`.
    2. Sidecar reasserts False on load (refuses to start if tampered).
    3. MC's API schema-rejects `live_trading_enabled=True` (422).
  - **DTD vs PRD mode guard** — DTD-mode brains may ship
    `training_signal=True` (replay learning OK); PRD-mode brains
    cannot (live data poisoning prevention; 422 if attempted).
  - **Confidence-delta clamp** — server hard-caps `confidence_delta`
    at ±0.25. Raw value + clamp flag preserved in history so the
    operator can see brains hammering against the cap.
  - **Patch kit** at `/app/runtime_patch_kit/sovereign/`:
    * `wild_adaptive_core_v2.py` — operator's deterministic core,
      doctrine-patched.
    * `mc_client.py` — stdlib HTTPS client (`urllib.request`); posts
      stances + contributions + heartbeats to MC.
    * `local_state.py` — JSON-on-disk persistence with atomic writes.
    * `sidecar.py` — long-lived runner (`python sidecar.py --brain
      alpha --mode DTD`).
    * `STATE_SCHEMA.md` — wire-format spec for the local file +
      contribution snapshot + MC-side enrichments.
    * `README.md` — full deployment guide with required env vars.
    * `smoke_test.py` — 8/8 PASS doctrinal smoke tests (no MC
      connection required).
  - **MC backend**: `shared/sovereign_mode_guard.py` ingests
    contributions, snapshots seat policy + epoch on every receipt,
    persists to three collections (`sovereign_state` latest snapshot,
    `sovereign_state_history` immutable history,
    `sovereign_audit_log` operator timeline).
  - **Endpoints**:
    * `POST /api/runtime-discussion/sovereign/contribution` —
      brain sidecars ingest snapshots (runtime token auth).
    * `GET /api/admin/sovereign/state` — list latest snapshot per brain.
    * `GET /api/admin/sovereign/state/{brain}` — detail + 20-row
      history tail.
    * `GET /api/admin/sovereign/audit` — operator timeline, filter
      by brain.
  - **Frontend**: `SovereignTile.jsx` on `/runtime/{brain}` shows
    mode (DTD/PRD badge), posted_as seat, learning_rate,
    confidence_delta (red + raw value when clamped), weights bar
    chart (-3 ↔ +3 range, color by sign), recent-outcomes win/loss
    ribbon.
  - **Tests**:
    * `/app/backend/tests/test_sovereign.py` (22/22 PASS) — happy
      path, live_trading_enabled rejection, PRD+training_signal
      rejection, weight bounds, feature cap, runtime-token auth,
      delta clamping (positive/negative/in-range/infinity),
      operator-read JWT enforcement, seat-policy snapshot capture,
      history preserves raw delta + clamp flag.
    * 4 regression fixes in `tests/test_risedual_backend.py`
      (overview + diagnostics now accept ≥3 runtimes since REDEYE
      was promoted to full seat earlier).
  - **Total backend pytest = 217/217** (existing 195 + 22 sovereign).
- **Quorum awareness + memory provenance** (2026-02-12)
  - **Doctrine added**: each seat in `SEAT_POLICY` carries a
    `seat_required` bit. Defaults: `executor`, `governor`, and
    `opponent` are required; `decider` and `advisor` are informational.
    The required bits are what surface adversarial / governance
    blindness when the brain in that seat goes silent.
  - **Quorum block on every position** (computed in `_hydrate`):
    - `seats_engaged` (the seats that have stamped)
    - `seats_required` (the seats marked required)
    - `seats_missing` (required but unstamped)
    - `vacant_required_seats` (required seats with NO brain assigned —
      worse than just unstamped; there's literally no one to ask)
    - `adversarial_blindness: bool` (opponent silent)
    - `governance_blindness: bool` (governor silent)
    - `degraded: bool` (any required seat unstamped)
  - **Frontend**: red/amber quorum stripe at the top of every degraded
    position card showing the exact failure mode + which seats are
    missing. Adversarial blindness uses red (the loud failure); pure
    governance blindness uses amber.
  - **Memory provenance** (B): every stance accepts two optional fields:
    * `memory_sources: list[str]` (≤ 32 entries, each ≤ 128 chars) —
      which memory artefacts shaped this stance. Empty list valid.
    * `confidence_origin: dict[str, float]` (≤ 12 keys, each value in
      [-1, 1]) — confidence decomposition (model / memory /
      contradiction_penalty / regime_alignment / …).
  - Validated at the schema layer (422 on out-of-range or oversized
    payloads). Persisted on the stance doc; surfaced on each brain's
    stance card on the Positions page as `MEMORY · src_a · src_b · …`
    and `ORIGIN · model: +0.71 · memory: +0.12 · contradiction: -0.09`
    (negative contributions shown in red so you can spot which factors
    pulled the confidence DOWN).
  - **Tests**: `/app/backend/tests/test_quorum_and_provenance.py`
    (11/11 PASS) — fresh position has all required seats missing,
    opponent-silent flags adversarial_blindness, governor-silent flags
    governance_blindness, full quorum clears all flags, vacant required
    seat surfaced separately, stance persists memory_sources + origin,
    provenance is optional (empty defaults), out-of-range origin value
    rejected, > 32 memory_sources rejected, > 12 origin components
    rejected, operator path also supports provenance.
  - Total backend pytest = **195/195**.
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

**Next Action Items**
- **risedual.ai integration** is unblocked end-to-end: operator copies the kit from
  `/app/runtime_patch_kit/risedual_public/` to risedual.ai's repo, sets `MC_BASE_URL`
  + `MC_PUBLIC_TOKEN` env vars, swaps pages per `SWAP_NOTES.md`. Recommended order:
  Digest → Heatmap → Signals → War Room → Agent Activity → Models Mind → Sectors →
  Market Overview narrative → RiseDualGPT chat.

**P1 / P2 — Backlog**
- **P2 — Build 2 demote/freeze workflow**: operator-initiated downgrade + hard-freeze
  endpoints, both audit-logged. On hold pending Build 3 production verification.
- **P2 — Notifications (Slack/Email)** for `awaiting_second_sign` on promotions.
- **P2 — Real-time updates (websocket)** for receipts + diagnostics.
- **P2 — Drop-in slots** for real Alpha/Camaro/Chevelle code (folder layout already
  mirrors the eventual import points).
- **P2 — Sector ETF feeder** — would lift `/api/public/sectors` out of degraded.
- **P3 — Phase 3 Public-API extensions**: `/public/admin/kill-switch` (admin-tier
  surfacing), Stripe-flow telemetry from risedual.ai → MC, dashboard for per-tier
  request rates against `/api/public/*`.

## User Personas
- **Operator (Admin)** — single seeded role today. Reads dashboards, observes
  receipts, validates that all stacks remain in observation mode.

## Test Credentials
See `/app/memory/test_credentials.md`.
