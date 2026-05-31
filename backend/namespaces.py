"""Centralized collection naming. Importing from here is the only sanctioned way.
Crossing namespaces (e.g. alpha reading camaro_shadow_rows) is a doctrine violation."""

# Shared infrastructure (the "shared nervous system")
SHARED_RECEIPTS = "shared_adl_receipts"
SHARED_MEMORY = "shared_labeled_memories"
SHARED_CALIBRATORS = "shared_calibrators"
SHARED_FEATURE_BUILDERS = "shared_feature_builders"
SHARED_ARTIFACTS = "shared_artifact_inventory"

# Per-runtime decision authorities (the three separate brains)
ALPHA_DECISION_LOG = "alpha_decision_log"
CAMARO_SHADOW_ROWS = "camaro_shadow_rows"
CHEVELLE_MEMORY_LABELS = "chevelle_memory_labels"
# 2026-05-29: RedEye now has its own per-brain decision log (parity
# with the other three brains). MC's diagnostics column reads from
# here for an apples-to-apples intent count. Contract owned by the
# RedEye team — see /app/memory/MC_HANDOFF_redeye_decision_log.md.
REDEYE_DECISION_LOG = "redeye_decision_log"

# Heartbeats from runtime sidecars (one upserted doc per runtime)
SHARED_HEARTBEATS = "shared_heartbeats"

# Sidecar check-ins (2026-02-XX) — Portable Survival Layer companion.
# Each sidecar (alpha, camaro, chevelle, redeye) POSTs its boot-time
# RuntimeStamp here. MC validates against the PROD doctrine, persists
# the latest stamp + verdict per runtime, and exposes a live "who's
# PROD vs preview" view on Diagnostics. One upserted doc per runtime.
# Distinct from `shared_heartbeats`: heartbeats prove liveness;
# check-ins prove identity (env_name, policy_hash, git_sha, etc.).
SIDECAR_CHECKINS = "sidecar_checkins"

# Authority + promotion (governed role evolution)
SHARED_AUTHORITY_STATE = "shared_authority_state"           # one doc per runtime, history embedded
SHARED_PROMOTION_ARTIFACTS = "shared_promotion_artifacts"   # Patent G evidence emitted by runtimes
SHARED_PROMOTION_PROPOSALS = "shared_promotion_proposals"   # pending operator countersign

# Cross-brain discussion layer (mediated; pull-only on consumers)
SHARED_OPINIONS = "shared_brain_opinions"                   # threaded discussion across brains
SHARED_OUTCOMES = "shared_brain_outcomes"                   # operator/chevelle-resolved outcomes per opinion
SHARED_CONFLICTS = "shared_brain_conflicts"                 # auto-detected disagreement pairs

# Pattern snapshots (2026-05-27) — descriptive evidence layer.
# Per (source, symbol, tf, last_bar_ts) snapshot of the
# `shared.patterns.base_breakout` detector. Brains pull via the
# existing technical feed; this collection makes detections
# REPLAYABLE for training (Shelly substrate) and historical audit.
# Doctrine: PURE EVIDENCE. No authority. No gate. No execution
# implication. Brains read; seat holder acts.
SHARED_PATTERN_SNAPSHOTS = "shared_pattern_snapshots"

# Shared technical evidence (OHLCV + indicator snapshots; pull-only)
# Doctrine: the technical layer is shared evidence — same bars, four
# brains read it, each forms its own opinion. No brain owns it.
SHARED_OHLCV_BARS = "shared_ohlcv_bars"
SHARED_INDICATOR_SNAPSHOTS = "shared_indicator_snapshots"

# Data Stack Phase 1 (2026-05-27) — market-data + alt-data layer.
# Doctrine: All data is EVIDENCE. Brains read it; brains weight it;
# seat holder acts. MC verifies feed integrity (auth, schema,
# freshness) but never evaluates trade quality based on the data.
# Adding a new provider must not introduce any execution-authority
# path. The OHLCV ingest schema must continue to reject any
# `may_execute` field.
SYMBOL_METADATA = "symbol_metadata"          # per-symbol float, market cap, sector
PATTERNS_UNIVERSE = "patterns_universe"      # operator-managed watchlist
FEEDER_HEALTH_AUDIT = "feeder_health_audit"  # per-feeder 429/error rolling log

# Daily Market Snapshots (2026-06-XX) — three frozen, point-in-time
# views of the S&P-500 equity universe per trading day. Captured at
# 09:35 / 12:30 / 16:05 ET. Doctrine: DERIVED EVIDENCE ONLY. Brains
# poll for retrieval; MC never executes off these. Retention is N
# trading days (default 5); the wipe pass runs at the start of each
# new trading day. Index: (market_day, label, symbol).
DAILY_MARKET_SNAPSHOTS = "daily_market_snapshots"
# Captured-batch audit row (one per capture run, regardless of how
# many symbols had bars). Lets the operator confirm "Yes, MC did
# fire the 09:35 capture today" even if every symbol came back null.
DAILY_SNAPSHOT_CAPTURE_LOG = "daily_snapshot_capture_log"
ALT_DATA_FILINGS = "alt_data_filings"        # SEC EDGAR Form 4 / filing index
ALT_DATA_MACRO = "alt_data_macro"            # FRED series cache


# Kraken Pro connection — encrypted credential storage + execution toggle.
# Single-tenant: at most one connected key set lives here. Stored as one
# document with key `"singleton"`. Doctrine: keys store reads-only data by
# default; `execution_enabled` defaults False and must be flipped by the
# operator with an audit-logged action.
KRAKEN_CREDENTIALS = "kraken_credentials"
KRAKEN_AUDIT_LOG = "kraken_audit_log"

# Brain Roster — dynamic role assignment across the four brains.
# Doctrine: the roster is descriptive metadata. Assigning Camaro to
# "executor" does NOT grant Camaro execution authority. `may_execute`
# remains schema-pinned False on every endpoint, every patch kit. The
# roster simply records "if execution were enabled, this is the brain
# the operator currently trusts in that seat." Roles: decider, executor,
# governor, advisor. Defaults match the original doctrine; swappable on
# demand by the operator.
BRAIN_ROSTER = "brain_roster"
ROSTER_AUDIT_LOG = "roster_audit_log"
# Per-(brain, role) eligibility matrix. Singleton doc keyed "current".
# Doctrine: operator-controlled access list deciding WHICH seats each
# brain is allowed to hold. Like the roster itself, this is descriptive
# (it doesn't grant execution; it constrains role assignment).
BRAIN_ELIGIBILITY = "brain_eligibility"

# IBKR Web API connection — encrypted access_token storage. Singleton.
# Doctrine: same as Kraken — `execution_enabled` defaults False, all
# trade endpoints intentionally not wired in this phase.
IBKR_CREDENTIALS = "ibkr_credentials"
IBKR_AUDIT_LOG = "ibkr_audit_log"

# Public.com connection — two-step auth. We store the operator's
# long-lived SECRET KEY (Fernet-encrypted) and cache short-lived
# ACCESS TOKENS (also encrypted) along with the expiry timestamp; the
# client refreshes on demand and on a periodic schedule. Doctrine: same
# as Kraken/IBKR — Phase 1 is read-only; `execution_enabled` defaults
# False and trade endpoints are intentionally not wired.
PUBLIC_CREDENTIALS = "public_credentials"
PUBLIC_AUDIT_LOG = "public_audit_log"

# Lane Execution Toggles (2026-02-18) — operator-controlled kill switch
# per-lane (equity, crypto). Decoupled from broker credential state:
# keys can stay connected while execution is paused, and vice versa.
# Singleton doc keyed `"current"`. Defaults to ALL OFF — execution is
# explicitly opted into. Every flip is audit-logged.
LANE_EXECUTION_TOGGLES = "lane_execution_toggles"

# Observation Receipts (2026-02-18, ladder doctrine) — graded
# learning samples from "honest hold" intents (brain emitted
# directional label but self-zeroed the size). Synthetic; no broker.
# Resolved against market price by a later worker.
OBSERVATION_RECEIPTS = "observation_receipts"

# Learning Ladder (2026-02-18, Phase 3) — per-(brain, lane) promotion
# stage tracker. Stages: observation_only → micro_paper → micro_live
# → normal_live. Defaults observation_only. Operator-promotable.
LEARNING_LADDER = "learning_ladder"
LEARNING_LADDER_AUDIT = "learning_ladder_audit"

# Execution receipts namespace alias for the ladder counter (reads
# fills tagged execution_mode="ladder_paper" / "ladder_live").
EXECUTION_RECEIPTS = "execution_receipts"

LANE_EXECUTION_AUDIT_LOG = "lane_execution_audit_log"

# Broker Freeze — emergency kill switch above the lane toggles (2026-05-23).
# Blocks ALL broker submit paths regardless of lane, credentials, or
# gate state. Singleton doc keyed "current". Defaults to UNFROZEN when
# the collection is empty.
BROKER_FREEZE_STATE = "broker_freeze_state"
BROKER_FREEZE_AUDIT_LOG = "broker_freeze_audit_log"

# Broker reconciliation — one row per `broker_orders` row reconciled
# against MC's internal records (`shared_intents`, `execution_receipts`).
# Tags unmatched orders as UNVERIFIED_BROKER_EXECUTION so the kernel
# permanently refuses to train on them.
BROKER_RECONCILIATION = "broker_reconciliation"

# Sovereign contribution attempt log (2026-05-24).
# Every POST to /api/runtime-discussion/sovereign/contribution writes
# one row here — both 200 successes AND 422 rejections — so the
# operator panel can show split counters (pushed / rejected / errored)
# canonically from MC's side. Brains' self-reported counters can't
# be trusted in the failure-mode case (a sidecar that's failing to
# serialize can't accurately count its own failures).
SOVEREIGN_CONTRIB_ATTEMPTS = "sovereign_contribution_attempts"

# Brain memory ingest (2026-05-24).
# Brains write resolved decision memories here (one row per resolved
# decision: outcome + realized_r + features) so MC can render corpus
# health on the operator dashboard and downstream consumers can train
# against the same canonical store. Distinct from `shared_intents`
# (forward-looking) and `execution_receipts` (broker-confirmed fills).
# Idempotent insert keyed on `(brain, memory_id)`.
BRAIN_MEMORIES = "brain_memories"
BRAIN_MEMORY_INGEST_AUDIT = "brain_memory_ingest_audit"



# Position primitive (2026-02-11) — discrete thesis object the 4 brains
# discuss. Every brain stamps a stance (long / short / abstain) with
# confidence + notes; the brain in the executor seat (per Roster) makes
# the final long/short call. Phase 1 is discussion only — no order
# placement, no broker side-effects.
SHARED_POSITIONS = "shared_positions"
SHARED_POSITION_STANCES = "shared_position_stances"
SHARED_POSITION_AUDIT = "shared_position_audit"

# Seat-holder nudges (2026-05-30) — operator pings the brain CURRENTLY
# holding a missing/silent seat on a specific position. Read-only
# observability for the brain side (poll-able via runtime-token).
# Cooldown-throttled to prevent spam. Authority: ADVISORY ONLY — does
# not affect any gate, seat assignment, or execution authority.
SEAT_NUDGES = "seat_nudges"

# Decision Machine — intent envelopes (brain-emitted), gate audit log
SHARED_INTENTS = "shared_intents"                          # brain-emitted decision intents
SHARED_GATE_RESULTS = "shared_gate_results"                # one row per gate check on an intent
SHARED_GOVERNANCE_DECISIONS = "shared_governance_decisions"  # per-intent governance verdict + dissent log
SHARED_EXECUTOR_SEAT = "shared_executor_seat"              # single-row registry: who holds the executor seat
SHARED_EXECUTOR_ROTATIONS = "shared_executor_rotations"    # append-only audit log of seat rotations

# Auditor seat — mirrors the Executor seat. Rotates separately. The brain
# holding this seat plays the contrary-case "what could go wrong" role
# on every hypothesis analysis. Default empty; operator rotates.
SHARED_AUDITOR_SEAT = "shared_auditor_seat"
SHARED_AUDITOR_ROTATIONS = "shared_auditor_rotations"

# Hypothesis analyses — operator-triggered dual-narrative reports (Strategist + Auditor)
# for a ticker. Audit-logged so we can review what the brains said about
# a symbol over time. Not exposed to the public API.
HYPOTHESIS_ANALYSES = "hypothesis_analyses"

# Alpaca paper broker — Fernet-encrypted key pair + audit log.
# Singleton credential doc keyed "singleton". Doctrine: paper only;
# `execution_enabled` defaults True (paper is safe), live broker is a
# separate adapter behind a dual-sign promotion gate.
ALPACA_CREDENTIALS = "alpaca_credentials"
ALPACA_AUDIT_LOG = "alpaca_audit_log"

# Execution receipts — one row per intent that passed the gate chain and
# was routed to a broker. Read by the exposure-caps daily-spend tally,
# the operator receipts page, and the outcome broadcast (later).
EXECUTION_RECEIPTS = "execution_receipts"

# ─── Live position lifecycle (2026-02-16) ───────────────────────────────
# One row per FILLED order. Lifecycle: open → managing → closed. State
# transitions are recorded via mc_shelly (event_types: position_opened,
# position_managing, position_closed) AND broadcast to SHARED_OUTCOMES
# on close so the outcome scorers (calibration, brier, regime breakdown)
# pick up every executed trade automatically.
SHARED_LIVE_POSITIONS = "shared_live_positions"
SHARED_LIVE_POSITION_AUDIT = "shared_live_position_audit"

# ─── Per-brain × lane intent-emission policy (2026-02-16) ──────────────
# Independent of the seat eligibility matrix. Eligibility governs WHICH
# SEATS a brain may hold; this collection governs whether a brain may
# even POST an intent into a given lane. Set `{brain, lane,
# allowed:false}` to 403 every incoming intent from that (brain, lane)
# pair at the ingest layer — useful when an engine is misbehaving and
# the operator wants to mute it without touching the sidecar.
BRAIN_LANE_POLICY = "brain_lane_policy"

# ─── Position Monitor scheduler — risk-guard evaluation log (2026-02-17) ─
# Append-only stream of every guard evaluation the Position Monitor
# loop performs. Lets the operator audit which guard fired (or held)
# on which position and why, without re-running the math. Rows are
# created by `shared/risk/position_monitor.py:_log_evaluation`.
RISK_MONITOR_EVALUATIONS = "risk_monitor_evaluations"

# ─── Brain doctrine sidecar packet audit log (2026-02-17) ─
# Append-only stream of `BRAIN_DOCTRINE_SIDECAR_PACKET` events emitted
# by the equity intent ingest path. Each row is the complete shared
# `DoctrineLabels` + four brain-flavored interpretations, joined to the
# intent_id and ingest timestamp. Read-only training substrate for
# Shelly — never consulted by the gate chain. See
# `shared/intents.py:_build_and_persist_doctrine_packet`.
DOCTRINE_SIDECARS = "doctrine_sidecars"

# ─── Verified Reinforcement Layer (2026-02-16) ──────────────────────────
# VRL records two kinds of evidence:
#   1. Per-receipt verifications: post-fill slippage / drift checks so the
#      operator can audit how faithfully the broker honored the intent.
#   2. Per-gate scorecards: rolling true-positive / false-positive tallies
#      for every gate in the chain, so the operator can see which gates
#      reliably block losers vs. block winners (i.e. which gates are
#      learning vs. just adding friction).
SHARED_VRL_VERIFICATIONS = "shared_vrl_verifications"
SHARED_VRL_SCORECARDS = "shared_vrl_scorecards"

# MC Shelly — Mission Control's own labeled memory. Every meaningful
# event MC observes (intent ingested, gate pass/fail, order routed,
# position open/close) is recorded here with the brain's full position
# snapshot at the time of the event. This is the training-data substrate
# for future brain training pipelines. Append-only.
MC_SHELLY = "mc_shelly"

# Sovereign sidecar contributions — periodic snapshots of each brain's
# internal deterministic state (weights, learning rate, recent outcomes).
# `SOVEREIGN_STATE` is one doc per brain (latest snapshot).
# `SOVEREIGN_STATE_HISTORY` is immutable: one row per contribution, kept
# for replay + drift analysis. `SOVEREIGN_AUDIT_LOG` is the operator-
# readable timeline of contributions, mode flips, and clamp events.
SOVEREIGN_STATE = "sovereign_state"
SOVEREIGN_STATE_HISTORY = "sovereign_state_history"
SOVEREIGN_AUDIT_LOG = "sovereign_audit_log"

# Public-API LLM features (Phase 2 — Direction C).
# Narrative cache memoizes the digest LLM summary for a short window so
# we don't burn tokens on every dashboard refresh. The Pro Max chat
# endpoint was retired 2026-02-16 — the main risedual.ai site hosts the
# chat surface directly; MC is admin-only and no longer needs to be a
# chat backend. The `public_chat_messages` collection is left in place
# but is no longer written to; the operator can drop it manually.
PUBLIC_NARRATIVE_CACHE = "public_narrative_cache"

# Per-request log for the operator-only traffic verification page
# (/public-traffic). One row per /api/public/* call: endpoint, tier,
# status, latency_ms, timestamp. Bounded via TTL index (24h default).
PUBLIC_REQUEST_LOG = "public_request_log"

# Rate-limit counters per (tier, minute-bucket). One doc per tier per
# minute, $inc atomically. TTL index drops docs after 2 minutes so the
# collection stays tiny regardless of traffic.
PUBLIC_RATE_LIMITS = "public_rate_limits"

# ───────────────────────────────────────────────────────────────────────
# PARADOX_RECORDS — the audit artifact produced by every gated intent.
# ───────────────────────────────────────────────────────────────────────
#
# Doctrine: AUDITOR is not a seat. The audit is the emergent record
# of the executor's call AND the opponent's challenge (or shadow
# observation). The kernel writes one paradox_record per gated intent,
# preserving the tension between the two voices without picking a side.
#
# Schema (lightweight; produced by the kernel, append-only):
#   intent_id              : str  — the gated intent's id
#   executor_runtime       : str  — anchored runtime ("camaro")
#   executor_call          : dict — direction, confidence, snapshot ref
#   opponent_runtime       : str  — anchored runtime ("redeye")
#   opponent_mode          : str  — live | shadow_observation | offline
#   opponent_challenge     : dict | None — challenge payload (None if offline)
#   kernel_verdict         : str  — APPROVED | REJECTED | DAMPENED
#   audit_status           : str  — final | shadow | unaudited
#   created_at             : datetime
#
# `audit_status` is the operator-facing accountability surface:
#   final      → opponent was live and weighed in
#   shadow     → opponent was in observation mode; trade fired anyway
#   unaudited  → opponent was offline; operator must be aware
PARADOX_RECORDS = "paradox_records"

# ─── PARADOX wake orders (2026-02-XX) ─────────────────────────────────
# Operator-issued "process this ticker now" commands targeted at one
# brain (or all four). MC writes the order with a signed JWT envelope;
# the sidecar polls `/api/admin/paradox/wake-orders/{brain}` on its
# normal heartbeat cadence, verifies the signature, processes the
# ticker, and POSTs back to `…/ack` to consume it. This is the
# operator's "wake up and look at SYMBOL" panic-button: it does NOT
# bypass any execution gate — the brain still has to produce a valid
# intent that passes the gate chain.
#
# Schema (one doc per issued order, append-only):
#   order_id      : str   — uuid4
#   brain         : str   — one of LIVE_RUNTIMES
#   ticker        : str   — uppercase symbol
#   note          : str   — operator's optional note
#   signed_token  : str   — HS256 JWT, claims {order_id, brain,
#                            ticker, issued_at, exp, kind:"wake"}
#   issued_by     : str   — operator email
#   issued_at     : datetime
#   expires_at    : datetime  (issued_at + WAKE_ORDER_TTL_SECONDS)
#   status        : "pending" | "acked" | "expired"
#   acked_at      : datetime | None
#   ack_note      : str   — sidecar's optional ack note
PARADOX_WAKE_ORDERS = "paradox_wake_orders"

# ─── LLM_CALLS — RISE_AI Model Adapter Kernel decision-trace ledger ──
# Doctrine pin (2026-02-XX): every call routed through `shared/llm/`
# lands here. This collection IS the moat. Rows carry the full prompt,
# response, role, task, provider, model, latency, and an explicit
# `llm_authority: "ADVISORY_ONLY"` stamp. Never trim this field.
#
# Schema (best-effort writes; ledger failures must not break the
# brain's LLM call):
#   call_id              : str (uuid4)
#   session_id           : str
#   role                 : str
#   task                 : str
#   provider             : "openai"|"anthropic"|"gemini"|"local"
#   model                : str
#   ok                   : bool
#   error                : str | None
#   prompt               : str  (clipped to 200KB)
#   response             : str  (clipped to 200KB)
#   prompt_bytes, response_bytes, prompt_truncated, response_truncated
#   usage                : dict (provider-shaped; may be empty)
#   metadata             : dict
#   latency_ms           : int
#   llm_authority        : "ADVISORY_ONLY"   (stamped — never mutate)
#   kernel_version       : str
#   git_sha              : str
#   created_at           : ISO datetime str
LLM_CALLS = "llm_calls"

# ─── RISE_AI training substrate (2026-02-XX) ──────────────────────────
# `LLM_PROVIDER_STATE` — operator-set promotion state per provider.
#   One doc per provider. Defaults live in
#   `shared/llm/routing_policy.py:DEFAULT_PROMOTION_STATE`.
#   Schema: {provider, state ∈ {SHADOW, ADVISOR, PRIMARY, OFFLINE}, note}
LLM_PROVIDER_STATE = "llm_provider_state"

# `LLM_PREFERENCE_LOG` — brain post-hoc grades on LLM answers.
#   Append-only. Multiple grades per call_id are allowed.
#   Schema: {call_id, score ∈ [-2..2], outcome, note, grader, created_at}
LLM_PREFERENCE_LOG = "llm_preference_log"

# `LLM_DISTILLATION_QUEUE` — successful (prompt, response, outcome)
# triples queued for training the self-trained model. Idempotent on
# call_id. `consumed_at` stamps when the trainer pulled the row;
# rows are never deleted (audit trail of what was learned from).
LLM_DISTILLATION_QUEUE = "llm_distillation_queue"

# `LLM_EVAL_RUNS` — candidate-vs-primary head-to-head from
# `shared/llm/training/eval_harness.py`. One doc per run; each
# embeds the full per-prompt detail + a summary block.
LLM_EVAL_RUNS = "llm_eval_runs"

# ─── Paradox Coordinator v0 (2026-02-XX) ──────────────────────────────
# Doctrine: candidate generator + advisory evaluator. NO execution
# authority. Coordinator v0 NEVER posts to /api/execution/submit on
# its own — human/admin promotion still required.
#
# `PARADOX_WATCHLIST` — operator-curated symbol list (primary universe
# for /paradox/scan). Schema:
#   {symbol, lane ∈ {equity, crypto}, active: bool, added_by,
#    added_at, note?}
PARADOX_WATCHLIST = "paradox_watchlist"

# `PARADOX_CANDIDATES` — output of /paradox/scan. Append-only.
# Schema:
#   {candidate_id (uuid4), symbol, lane, source, status, reason,
#    snapshot ({price, volume, spread_bps, rvol, halted}),
#    filter_pass: bool, filter_failures: list[str],
#    created_at, evaluated_at|None, evaluation_id|None}
PARADOX_CANDIDATES = "paradox_candidates"

# `PARADOX_RETRAIN_RECOMMENDATIONS` — output of
# /paradox/ml/retrain/check when any trigger fires. Append-only,
# operator-consumed. NEVER auto-promotes. Schema:
#   {rec_id, triggers: list[str], stats, recommended_target,
#    created_at, consumed_at|None, consumed_by|None}
PARADOX_RETRAIN_RECOMMENDATIONS = "paradox_retrain_recommendations"

# ─── RISE_AI saved threads (2026-02-XX) ───────────────────────────────
# Doctrine pin: reasoning memory ONLY. NOT execution memory, NOT trade
# authority, NOT doctrine authority. Threads persist transcripts so
# the operator can resume long-running reasoning sessions; the LLM
# kernel uses `session_id` to keep context.
#
# `RISE_AI_THREADS` — thread metadata, one doc per thread.
#   {thread_id (uuid4), title, session_id, mode, role,
#    pinned: bool, tags: list[str], message_count: int,
#    last_call_id, created_at, updated_at, created_by, archived: bool}
RISE_AI_THREADS = "rise_ai_threads"

# `RISE_AI_THREAD_MESSAGES` — append-only transcript rows. One doc
# per message. Indexed by (thread_id, seq).
#   {thread_id, seq: int, kind ∈ {"user","rise"}, text, mode, role,
#    call_id, provider, model, latency_ms, llm_authority, extra,
#    created_at}
RISE_AI_THREAD_MESSAGES = "rise_ai_thread_messages"

RUNTIMES = ("alpha", "camaro", "chevelle", "redeye")

# ───────────────────────────────────────────────────────────────────────
# PARADOX hierarchy (2026-05-20)
# ───────────────────────────────────────────────────────────────────────
#
# Architectural pin: the kernel sits ABOVE the named brains, not as a
# peer. Each ROLE is anchored to exactly one runtime — no Cartesian
# eligibility matrix, no per-lane swap dance, no role/runtime
# conflation.
#
#   RISEDUAL                    (platform)
#     PARADOX (MC kernel)       (the system mind; verifies, routes, signs)
#       Alpha     → strategist
#       Camaro    → executor
#       Chevelle  → governor
#       REDEYE    → opponent
#       Shelly    → memory
#
# AUDITOR is intentionally absent. The audit is an EMERGENT FUNCTION
# of (executor, opponent) — the paradox_record artifact written on
# every gated intent. The kernel preserves the tension between the
# two voices and signs the result. There is no auditor seat.
#
# Tripwire (`test_paradox_role_anchors_locked` in
# tests/test_paradox_namespace.py) refuses any drift of this table —
# adding new entries or aliasing is a doctrine violation. The whole
# point of fixing the anchors is that they DO NOT move.

PARADOX_KERNEL = "PARADOX"  # the system mind; the kernel above the brains

ROLE_ANCHORS: dict[str, str] = {
    "strategist": "alpha",
    "executor":   "camaro",
    "governor":   "chevelle",
    "opponent":   "redeye",
    "memory":     "shelly",   # namespace-reserved; Shelly is conceptual today
}

# Reverse lookup: runtime → role. Generated, not hand-maintained.
RUNTIME_ROLE: dict[str, str] = {v: k for k, v in ROLE_ANCHORS.items()}

# Runtimes that are operationally live (have a sidecar that reports in).
# Shelly is reserved namespace but not yet a running sidecar.
LIVE_RUNTIMES: tuple[str, ...] = ("alpha", "camaro", "chevelle", "redeye")

# Opponent mode — REDEYE is currently re-learning in shadow mode.
# `live`               → opponent challenges gate trades
# `shadow_observation` → opponent observes only; trades fire; paradox_record
#                        stamps `audit_status=shadow` so the period is
#                        replayable when opponent returns to `live`
# `offline`            → opponent role is vacant; executor calls are
#                        UN-AUDITED — operator must be aware
OPPONENT_MODE_LIVE = "live"
OPPONENT_MODE_SHADOW = "shadow_observation"
OPPONENT_MODE_OFFLINE = "offline"


# Historical: REDEYE used to be an "advisor sidecar". We've since promoted
# it to a full seat (2026-02-11). Kept for back-compat with older patch
# kits that read this constant; new code should iterate RUNTIMES.
ADVISORS: tuple[str, ...] = ()

# Convenience set of every brain that may participate in the discussion layer.
DISCUSSION_PARTICIPANTS: tuple[str, ...] = RUNTIMES + ADVISORS

# ───────────────────────────────────────────────────────────────────────
# RUNTIME ROLES — TRAINING-INTENT metadata only.
#
# Doctrine pin (2026-02-17, rev3):
#   Authority lives on SEATS, not brains. The fields here describe
#   WHAT EACH BRAIN WAS TRAINED FOR — not what it is allowed to do.
#   Any code that reads these fields to GRANT or DENY execution is a
#   bug. Use the roster's seat assignment instead. The `allowed_actions`
#   field is retained for back-compat with diagnostic surfaces that
#   enumerate "what kind of work this brain produces", but it does
#   NOT enforce anything.
# ───────────────────────────────────────────────────────────────────────
ROLES: dict[str, dict] = {
    "alpha": {
        "role": "trader",
        "title": "Alpha",
        "tagline": "structured trader",
        "description": (
            "Trained on structured-signal day-trading. Stamps "
            "executable proposals; whether a proposal lands as an "
            "order depends on the SEAT holder of the moment, not on "
            "Alpha's identity."
        ),
        "allowed_actions": [
            "enter_long", "enter_short", "exit", "scale_in", "hold",
            "phase6_proposal",
        ],
    },
    "camaro": {
        "role": "challenger",
        "title": "Camaro",
        "tagline": "challenger / counterfactual",
        "description": (
            "Trained to attack the thesis and surface counterfactuals. "
            "Whether the challenge becomes a veto / reduce / watch "
            "depends on which seat Camaro currently holds."
        ),
        "allowed_actions": [
            "shadow_proposal", "counterfactual", "veto", "reduce", "watch",
            "executor_proposed",
        ],
    },
    "chevelle": {
        "role": "governor",
        "title": "Chevelle",
        "tagline": "memory + calibration",
        "description": (
            "Trained on memory firewall, readiness gate, calibration "
            "gate, audit verification, and promotion-control reasoning. "
            "Authority to act on those signals depends on which seat "
            "Chevelle currently holds."
        ),
        "allowed_actions": [
            "readiness_gate", "calibration_gate", "audit_verify",
            "promotion_decision", "authority_call",
        ],
    },
    "redeye": {
        "role": "opponent",
        "title": "REDEYE",
        "tagline": "adversarial scout",
        "description": (
            "Trained to argue the contrary case on every position. "
            "REDEYE speaks via the shared discussion layer and the "
            "position primitive — peers may read, never modify, its "
            "stances. Authority to translate a contrary-case finding "
            "into a veto / reduce / watch depends on the seat REDEYE "
            "currently holds."
        ),
        "allowed_actions": [
            "post_opinion", "read_opinions", "read_roles_manifest",
            "adversarial_advisory", "stance_contrary",
        ],
    },
}

# ───────────────────────────────────────────────────────────────────────
# AUTHORITY LADDER — what a runtime is currently ALLOWED to do.
# Evolves only via governed promotion (Patent G evidence + Patent J gate
# + operator countersign). Never flipped organically.
# ───────────────────────────────────────────────────────────────────────
AUTHORITY_LADDER: list[str] = [
    "observer",     # 0 — watches only, no recommendations
    "challenger",   # 1 — can recommend veto / reduce / watch
    "advisor",      # 2 — can influence sizing
    "co_trader",    # 3 — can propose executable trades
    "primary",      # 4 — can become execution leader
]
AUTHORITY_LEVEL: dict[str, int] = {s: i for i, s in enumerate(AUTHORITY_LADDER)}

# Off-ladder state for the Governor role (Chevelle). Cannot be promoted onto
# the trading ladder; cannot ever execute.
GOVERNOR_STATE = "governor"

# Authority states that grant execution authority.
EXECUTION_AUTHORITY_STATES = frozenset({"co_trader", "primary"})

# DEFAULT_AUTHORITY moved up — see right after the ROLES definition.

# Default authority per runtime on first boot. Promotion writes new history;
# the default is what we install when the doc is missing.
DEFAULT_AUTHORITY: dict[str, str] = {
    "alpha": "co_trader",     # only stack with execution authority today
    "camaro": "challenger",
    "chevelle": GOVERNOR_STATE,
    "redeye": "advisor",      # full-seat short-side advisor
}

# Patent J readiness thresholds (operator-tunable later).
# Bootstrap-friendly tuning 2026-02-17: lowered resolved-row floor and loosened
# calibration tolerances so early-fleet brains can clear the gate. Doctrine pins
# (role violations, toxic memory, heartbeat) stay at safety values.
PROMOTION_THRESHOLDS = {
    "ece_max": 0.10,                    # Expected Calibration Error
    "brier_max": 0.30,                  # Brier score
    "min_resolved_rows": 25,            # sample size floor (bootstrap)
    "min_disagreement_stability": 0.55, # stability of dissent over the window
    "max_toxic_memory_24h": 5,          # firewall quarantines in 24h
    "max_role_violations_24h": 0,       # zero tolerance — doctrine pin
    "heartbeat_max_age_seconds": 300,   # liveness floor
}

# Promotion countersign requirement (2026-02-17 doctrine):
# Solo-operator deployment. The propose call ALREADY required an authenticated
# admin JWT — that's the human sign. A second click on /countersign for the
# same human added zero safety. Set this False to auto-elevate inside /propose
# when Patent J readiness passes. When helpers are onboarded, flip to True
# and /propose will fall back to the pending-then-countersign flow.
REQUIRE_COUNTERSIGN = False

# Heartbeat staleness threshold for the dashboard alert. A runtime is "stale"
# if its last_seen is older than this. Tuned so a single missed heartbeat
# (45s loop) is forgiven, but two consecutive misses raise the alarm.
# Visibility-only — does NOT change authority, broker behavior, or receipt enforcement.
HEARTBEAT_STALE_AFTER_SECONDS = 90

# Operator three-tier heartbeat doctrine (2026-02-15). Visibility only.
#   < HEARTBEAT_OK_BELOW_SECONDS         → 🟢 healthy
#   ≥ HEARTBEAT_OK_BELOW_SECONDS and
#     < HEARTBEAT_PREVIEW_DRIFT_SECONDS  → 🟡 drift (could be cycle blip)
#   ≥ HEARTBEAT_PREVIEW_DRIFT_SECONDS    → 🔴 preview-drift — brain almost
#                                          certainly hitting the preview URL
#                                          instead of prod (operator rule).
# The dashboard surfaces these so an operator can spot URL drift fast.
HEARTBEAT_OK_BELOW_SECONDS = 60
HEARTBEAT_PREVIEW_DRIFT_SECONDS = 110


def runtime_can_execute_state(authority_state: str) -> bool:
    """Single source of truth: only authority states on the trading ladder
    that have reached co_trader or primary may execute."""
    return authority_state in EXECUTION_AUTHORITY_STATES


def is_on_ladder(authority_state: str) -> bool:
    return authority_state in AUTHORITY_LADDER


def next_authority(current: str) -> str | None:
    """Returns the next authority state up the ladder, or None if at top
    or off-ladder (governor)."""
    if current not in AUTHORITY_LEVEL:
        return None
    idx = AUTHORITY_LEVEL[current]
    if idx + 1 >= len(AUTHORITY_LADDER):
        return None
    return AUTHORITY_LADDER[idx + 1]
