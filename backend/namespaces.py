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

# Shared technical evidence (OHLCV + indicator snapshots; pull-only)
# Doctrine: the technical layer is shared evidence — same bars, four
# brains read it, each forms its own opinion. No brain owns it.
SHARED_OHLCV_BARS = "shared_ohlcv_bars"
SHARED_INDICATOR_SNAPSHOTS = "shared_indicator_snapshots"

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

# Position primitive (2026-02-11) — discrete thesis object the 4 brains
# discuss. Every brain stamps a stance (long / short / abstain) with
# confidence + notes; the brain in the executor seat (per Roster) makes
# the final long/short call. Phase 1 is discussion only — no order
# placement, no broker side-effects.
SHARED_POSITIONS = "shared_positions"
SHARED_POSITION_STANCES = "shared_position_stances"
SHARED_POSITION_AUDIT = "shared_position_audit"

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

RUNTIMES = ("alpha", "camaro", "chevelle", "redeye")

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
PROMOTION_THRESHOLDS = {
    "ece_max": 0.05,                    # Expected Calibration Error
    "brier_max": 0.20,                  # Brier score
    "min_resolved_rows": 100,           # sample size floor
    "min_disagreement_stability": 0.7,  # stability of dissent over the window
    "max_toxic_memory_24h": 5,          # firewall quarantines in 24h
    "max_role_violations_24h": 0,       # zero tolerance
    "heartbeat_max_age_seconds": 300,   # liveness floor
}

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
