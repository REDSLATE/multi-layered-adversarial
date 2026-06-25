"""RISEDUAL — Council Module.

Extracted 2026-02-15 from `shared/execution.py` (which had grown to
1355 lines). Contains the full lane-aware graduated council:
  * Lane policy table (equity / crypto)
  * Schema-tolerant brain & symbol matchers
  * Seat-bound lookups for governor & opponent (lane-aware)
  * `_governance_verdict` — pure function: graduated verdict matrix
  * `_evaluate_council` — orchestrator: composes governor + opponent +
    quantum-inspired regime overlay, writes the governance ledger row

This module exports the helpers `_evaluate_council` and the
diagnostic-endpoint helpers consume. See the module-level docstring of
each function for doctrine details.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import (
    SHARED_GOVERNANCE_DECISIONS,
    SHARED_RECEIPTS,
    SOVEREIGN_AUDIT_LOG,
)
from shared.mc_shelly import record_async
from shared.quantum_state import (
    BrainOpinion as _QSBrainOpinion,
    build_quantum_inspired_state as _build_quantum_state,
)
from shared.stack_personalities import (
    enrich_response as _stamp_personality,
    personality_of as _personality_of,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── council ─────────────────────────────
# Doctrine (rev4, 2026-02-15): LANE-AWARE GRADUATED COUNCIL.
#
# The council is bound to SEATS, not brain identities. The Governor seat
# holder must record a stance, but its DISSENT is now a smooth multiplier
# — not a binary kill switch. Hard veto stays available for true safety
# stops. Crypto runs a more permissive variant: it punishes hesitation
# more than equities, so governance damping is reduced and momentum
# weighting is lifted.
#
# Verdict outputs:
#   * HARD_VETO          → block (veto bit + governor conf ≥ hard_veto)
#   * NO_STANCE          → SOFT downweight (governor uncertain on symbol
#                          but alive) — NO LONGER a hard block
#   * GOVERNOR_OFFLINE   → block (no calls anywhere in 30m)
#   * SOFT_DISSENT       → executor fires at conf × dissent_conf_mult,
#                          size × dissent_size_mult. Below MIN_EXECUTOR_
#                          CONF_FLOOR after suppression → block.
#   * NO_DISSENT         → full size, lane base multiplier.
#
# Clamps prevent any single agent from collapsing or amplifying the
# action space beyond MAX_DOWNWEIGHT / MAX_UPWEIGHT.

# How fresh a council signal must be to count.
_COUNCIL_FRESHNESS_SECONDS = 600  # 10 minutes
# How long the governor seat can be silent before we consider it offline.
_GOVERNOR_OFFLINE_THRESHOLD_SECONDS = 1800  # 30 minutes

# Lane-aware policy. Each lane's policy lives in its own subpackage:
#   shared/equity/council_policy.py — consensus-first / governance-heavy
#   shared/crypto/council_policy.py — momentum-biased / governance-light
#
# This file is the dispatcher. A lane-only change should require
# editing ONLY the policy file in that lane's subpackage — never this
# one. (2026-02-16 reorg.)
from shared.crypto.council_policy import CRYPTO_POLICY
from shared.equity.council_policy import EQUITY_POLICY

COUNCIL_POLICY: dict[str, dict] = {
    "equity": EQUITY_POLICY,
    "crypto": CRYPTO_POLICY,
}


# ─────────────── Governor-block taxonomy (2026-02-20 rewrite) ──────────
#
# Operator doctrine (2026-02-20):
#
#     Brain      = opinion only
#     Seat       = restriction authority
#     Governor   = modifier
#     RoadGuard  = hard stop
#
# Translation: the governor's job is to **size** an intent down or up,
# never to kill it. Only seat-policy (Shelly tier config, lane toggles,
# MC receipt seal, etc.) and RoadGuards (kill-switch, missing creds,
# legal blocks like PDT) may hard-block. Brain-derived dissent
# downsizes; it never freezes the action space.
#
# Prior to this patch, three governor reasons still hard-blocked:
#   * GOVERNOR_HARD_VETO       — brain in the governor seat saying "no"
#   * GOVERNOR_SEAT_VACANT     — no modifier appointed
#   * SOFT_DISSENT_BELOW_FLOOR — governor dissent × brain conf < floor
#
# All three are now downgraded to RISK_DOWN_ONLY: the trade still
# fires at a reduced size, the operator sees the governor signal in
# the post-mortem, and seat-policy + RoadGuard remain the only stop
# layers. Doctrine pin: "the brain is both pressing the gas and
# grabbing the brake" — this patch removes the brake from the brain.

# Only RoadGuards / structural blocks remain in FATAL. Governor-
# derived reasons are gone from this list (moved to SILENCE below).
FATAL_GOVERNOR_REASONS: frozenset[str] = frozenset({
    # Structural / safety stops the operator demanded keep blocking.
    # These are RoadGuards, NOT brain-owned vetoes:
    "KILL_SWITCH_ACTIVE",
    "BROKER_UNAVAILABLE",
    "AUTH_MISSING",
    "SYMBOL_UNRESOLVED",
    "MAX_EXPOSURE_EXCEEDED",
    "PDT_BLOCK",
    "DUPLICATE_POSITION",
})

# Reasons that were historically hard-blocks but per doctrine
# downgrade to RISK_DOWN_ONLY. `governor_risk_multiplier()` returns
# the sizing penalty for each. Operator can tune the multiplier per
# reason if specific signals merit more / less suppression.
SILENCE_GOVERNOR_REASONS: frozenset[str] = frozenset({
    "GOVERNOR_OFFLINE",
    "NO_STANCE_LOW_EFFECTIVE_CONF",
    "GOVERNOR_NO_STANCE",
    # 2026-02-20: per operator doctrine, all of these become
    # modifiers, not blockers.
    "GOVERNOR_HARD_VETO",
    "GOVERNOR_SEAT_VACANT",
    "SOFT_DISSENT_BELOW_FLOOR",
})

# Per-reason sizing penalty. Stronger governor signals → smaller
# size, but never zero. The hard-veto multiplier matches the
# historical doctrine_reject softening (`risk_mult *= 0.20`).
GOVERNOR_SILENCE_RISK_MULTIPLIER: float = 0.50    # offline / no stance
GOVERNOR_HARD_VETO_RISK_MULTIPLIER: float = 0.20  # was hard-block
GOVERNOR_VACANT_RISK_MULTIPLIER: float = 0.50     # no modifier → mild
GOVERNOR_DISSENT_FLOOR_RISK_MULTIPLIER: float = 0.20  # was hard-block


def governor_blocks_execution(reason: str | None) -> bool:
    """Only RoadGuard / structural reasons may stop execution. All
    governor-derived reasons are modifiers (per 2026-02-20 doctrine).
    """
    return str(reason or "").upper().strip() in FATAL_GOVERNOR_REASONS


def governor_risk_multiplier(reason: str | None) -> float:
    """Per-reason sizing penalty for governor signals that previously
    hard-blocked. Returns 1.00 (no penalty) for unknown reasons so
    the council baseline isn't surprised by new reason strings."""
    r = str(reason or "").upper().strip()
    if r == "GOVERNOR_HARD_VETO":
        return GOVERNOR_HARD_VETO_RISK_MULTIPLIER
    if r == "GOVERNOR_SEAT_VACANT":
        return GOVERNOR_VACANT_RISK_MULTIPLIER
    if r == "SOFT_DISSENT_BELOW_FLOOR":
        return GOVERNOR_DISSENT_FLOOR_RISK_MULTIPLIER
    if r in SILENCE_GOVERNOR_REASONS:
        return GOVERNOR_SILENCE_RISK_MULTIPLIER
    return 1.00


# ─────────────── Restriction-source audit tag (2026-02-20) ─────────────
#
# Every gate result is now stamped with which layer emitted the
# restriction. Lets the operator answer "who blocked this?" with a
# single DB query rather than parsing reason strings.
#
#   brain      — brain opinion gate (advisory only; never blocks)
#   seat       — seat-policy (Shelly tier, lane toggle, MC receipt)
#   governor   — governor seat (now modifier-only per 2026-02-20)
#   roadguard  — structural hard-stop (freeze, creds, PDT, exposure)
#   broker     — broker-side response (Webull/Kraken rejection)

RESTRICTION_SOURCE_BRAIN     = "brain"
RESTRICTION_SOURCE_SEAT      = "seat"
RESTRICTION_SOURCE_GOVERNOR  = "governor"
RESTRICTION_SOURCE_ROADGUARD = "roadguard"
RESTRICTION_SOURCE_BROKER    = "broker"


def restriction_source_for_reason(reason: str | None) -> str:
    """Bucket a gate reason string into one of the five layers. Used
    by `_result()` to stamp every governor verdict, and by the
    post-mortem aggregator to bucket non-council reasons too."""
    r = str(reason or "").upper().strip()
    if r in FATAL_GOVERNOR_REASONS:
        return RESTRICTION_SOURCE_ROADGUARD
    if r in SILENCE_GOVERNOR_REASONS or r in {
        "SOFT_DISSENT_DOWNWEIGHTED", "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT",
        "NO_GOVERNOR_DISSENT",
    }:
        return RESTRICTION_SOURCE_GOVERNOR
    return RESTRICTION_SOURCE_GOVERNOR  # default for council-emitted


def _policy_for_lane(lane: Optional[str]) -> dict:
    """Pick the council policy for an intent's lane. Equity is the safe
    default for legacy / lane-untagged intents."""
    if (lane or "").lower() == "crypto":
        return COUNCIL_POLICY["crypto"]
    return COUNCIL_POLICY["equity"]


def _clamp_size(size: float, policy: dict) -> float:
    """Bound size adjustments by lane-policy floor/ceiling."""
    return max(policy["MAX_DOWNWEIGHT"], min(policy["MAX_UPWEIGHT"], size))


def _clamp_agent_delta(base: float, adjusted: float, policy: dict) -> float:
    """No single agent may move size by more than ±MAX_SINGLE_AGENT_INFLUENCE
    from the base. Prevents Chevelle freeze spirals AND Camaro dominance spirals."""
    cap = policy["MAX_SINGLE_AGENT_INFLUENCE"]
    delta = adjusted - base
    delta = max(-cap, min(cap, delta))
    return base + delta


# Brain-identity fields a receipt might use. Engines vary; we accept all.
_BRAIN_FIELDS = ("runtime", "brain", "stack", "source", "from")

# Candidate symbol paths inside an authority_call doc.
_SYMBOL_PATHS = (
    "intent.symbol",
    "symbol",
    "payload.symbol",
    "data.symbol",
    "call.symbol",
)

# Candidate action/kind fields (and the value we accept).
_ACTION_FIELDS = ("action", "kind", "type", "event")
_AUTHORITY_CALL_VALUES = ("authority_call", "AUTHORITY_CALL", "authoritycall")
_CONTRIBUTION_VALUES = ("contribution", "Contribution", "CONTRIBUTION")


def _brain_id_variants(name: str) -> list[str]:
    """All identity variants we accept for a brain — case forms PLUS
    the legacy/canonical pair via the brain legend.

    Doctrine (2026-02-23 dual-field migration): receipts and seat
    assignments may carry the brain identity as either the legacy
    stack code (alpha/camaro/chevelle/redeye) OR the canonical
    brain_id (camino/barracuda/hellcat/gto). The council's brain-
    match clause MUST search both so a hellcat-held seat finds
    chevelle-authored receipts (and vice versa). Without this, the
    `_latest_governor_call` lookup silently misses every mirror that
    used the other identity form.
    """
    if not name:
        return []
    variants: set[str] = {name, name.lower(), name.upper(), name.capitalize()}
    # Lazy-imported so the council module stays cheap to import in
    # contexts that don't need the legend (some sidecars).
    try:
        from shared.brain_legend import (  # noqa: WPS433
            canonicalize_stack, LEGACY_TO_CANONICAL, DISPLAY_NAMES,
        )
        canonical = canonicalize_stack(name)
        if canonical:
            # Add the canonical brain_id + its display name.
            variants.add(canonical)
            variants.add(canonical.upper())
            variants.add(canonical.capitalize())
            display = DISPLAY_NAMES.get(canonical)
            if display:
                variants.add(display)
            # Add every legacy alias for this canonical brain (one
            # canonical may have many legacy aliases over time).
            for legacy, can in LEGACY_TO_CANONICAL.items():
                if can == canonical:
                    variants.add(legacy)
                    variants.add(legacy.upper())
                    variants.add(legacy.capitalize())
    except Exception:  # noqa: BLE001
        # Defensive: legend lookup must never break the council
        # path. Worst case we lose cross-form matching but keep
        # case-variant matching, which is what we had before.
        pass
    return list(variants)


def _brain_match_clause(brain: str) -> dict:
    """Match any of the known identity fields against any case variant
    of `brain`. Returns an `$or` Mongo clause."""
    variants = _brain_id_variants(brain)
    return {"$or": [
        {field: {"$in": variants}} for field in _BRAIN_FIELDS
    ]}


def _authority_call_clause() -> dict:
    return {"$or": [
        {field: {"$in": list(_AUTHORITY_CALL_VALUES)}}
        for field in _ACTION_FIELDS
    ]}


def _contribution_clause() -> dict:
    return {"$or": [
        {field: {"$in": list(_CONTRIBUTION_VALUES)}}
        for field in _ACTION_FIELDS
    ]}


def _symbol_clause(symbol: str) -> dict:
    return {"$or": [{path: symbol} for path in _SYMBOL_PATHS]}


def _extract(doc: dict, path: str):
    """Walk a dotted path through nested dicts. Returns None if absent."""
    cur = doc
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _normalize_governor_call(doc: Optional[dict]) -> Optional[dict]:
    """Pull the governor's signals — executable, veto, confidence, stance,
    reason, timestamp — from whichever shape the receipt uses."""
    if not doc:
        return None
    # Try common payload containers in priority order.
    for container_path in ("intent", "payload", "call", "data", ""):
        node = _extract(doc, container_path) if container_path else doc
        if not isinstance(node, dict):
            continue
        # We accept the node as the "governor payload" if ANY governance
        # signal lives there.
        signals = ("executable", "veto", "stance", "confidence")
        if any(k in node for k in signals):
            return {
                "executable": node.get("executable"),
                "veto": bool(node.get("veto", False)),
                "confidence": float(
                    node.get("confidence")
                    or node.get("calibrated_confidence")
                    or node.get("raw_confidence")
                    or 0.0
                ),
                "stance": (
                    node.get("stance")
                    or node.get("call")
                    or node.get("authority_call")
                    or ""
                ),
                "reason": (
                    node.get("execution_gate_reason")
                    or node.get("reason")
                    or node.get("gate_reason")
                    or "unspecified"
                ),
                "ts": doc.get("timestamp") or doc.get("ts") or doc.get("created_at"),
                "shape_container": container_path or "root",
            }
    return None


# ── Roster-bound seat lookups ─────────────────────────────────────────
async def _seat_holder(role: str, lane: Optional[str] = None) -> Optional[str]:
    """Current occupant of `role` in the live roster, or None if vacant.

    Lane-isolated (2026-05-17, rev2): seats are STRICTLY separated by
    lane. When `lane="crypto"` we look up ONLY the crypto seat
    (`crypto_executor` → `crypto`, otherwise `crypto_<role>`). If the
    crypto seat is vacant, the call returns None — there is NO fallback
    to the equity seat. The earlier fallback let an equity-Governor
    occupant silently govern crypto intents; that violated lane
    isolation. To run crypto, the operator must explicitly assign each
    crypto seat.

    Mapping: the `crypto_executor` role is stored as `crypto` in the
    roster (legacy name from when crypto was a single executor seat).
    Everything else uses the `crypto_<role>` prefix.

    Alias resolution (2026-02-19): old role names `decider`/`advisor`
    are rewritten via `SEAT_ALIASES` to `executor`/`auditor` before the
    roster lookup. Callers asking for the deprecated names continue to
    function — they're routed to the canonical seat occupant.
    """
    from shared.roster import get_roster  # noqa: WPS433
    from shared.seat_policy import normalize_seat  # noqa: WPS433

    # Normalize at the boundary — anything asking for `decider` gets
    # the `executor` occupant; anything asking for `advisor` gets the
    # `auditor` occupant. Pre-existing canonical names pass through.
    r = await get_roster()
    assignments = r.get("assignments") or {}

    if (lane or "").lower() == "crypto":
        crypto_role_raw = "crypto" if role == "executor" else f"crypto_{role}"
        crypto_role = normalize_seat(crypto_role_raw) or crypto_role_raw
        return assignments.get(crypto_role)

    canonical_role = normalize_seat(role) or role
    return assignments.get(canonical_role)


async def _latest_governor_call(symbol: Optional[str], lane: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent authority_call by the current Governor
    seat holder for `symbol`. Returns (None, None) if the seat is vacant.
    Lane-aware: for crypto intents reads `crypto_governor` first."""
    holder = await _seat_holder("governor", lane=lane)
    if not holder or not symbol:
        return holder, None
    query = {"$and": [
        _brain_match_clause(holder),
        _authority_call_clause(),
        _symbol_clause(symbol),
    ]}
    doc = await db[SHARED_RECEIPTS].find_one(query, {"_id": 0}, sort=[("timestamp", -1)])
    return holder, doc


async def _latest_governor_any_call(lane: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent authority_call by Governor for ANY symbol.
    Used to distinguish 'governor offline' from 'governor uncertain on this name'.
    Lane-aware."""
    holder = await _seat_holder("governor", lane=lane)
    if not holder:
        return holder, None
    query = {"$and": [_brain_match_clause(holder), _authority_call_clause()]}
    doc = await db[SHARED_RECEIPTS].find_one(query, {"_id": 0}, sort=[("timestamp", -1)])
    return holder, doc


async def _latest_opponent_contribution(lane: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """(holder, doc) — most recent sovereign contribution by Opponent seat.
    Lane-aware: for crypto intents reads `crypto_opponent` first."""
    holder = await _seat_holder("opponent", lane=lane)
    if not holder:
        return holder, None
    query = {"$and": [_brain_match_clause(holder), _contribution_clause()]}
    doc = await db[SOVEREIGN_AUDIT_LOG].find_one(query, {"_id": 0}, sort=[("ts", -1)])
    return holder, doc


def _doc_ts(doc: Optional[dict]) -> Optional[str]:
    if not doc:
        return None
    return doc.get("timestamp") or doc.get("ts") or doc.get("created_at")


def _is_fresh(ts: Optional[str], max_age_seconds: int = _COUNCIL_FRESHNESS_SECONDS) -> bool:
    if not ts:
        return False
    try:
        # Parse ISO; tolerate "Z" suffix.
        cleaned = str(ts).replace("Z", "+00:00")
        emitted = datetime.fromisoformat(cleaned)
        if emitted.tzinfo is None:
            emitted = emitted.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - emitted).total_seconds()
        return age <= max_age_seconds
    except Exception:  # noqa: BLE001
        return False


# ── The graduated verdict ─────────────────────────────────────────────
def _governance_verdict(
    intent: dict,
    gov_norm: Optional[dict],
    governor_alive: bool,
    governor_holder: Optional[str],
    policy: Optional[dict] = None,
) -> dict:
    """Pure function: given the intent, the normalized governor call,
    and a lane policy, return the verdict dict.

    Output:
      {
        allowed: bool,
        reason: code,
        disagreement: bool,
        record_pushback: bool,
        risk_multiplier: float (continuous, clamped),
        effective_conf: float (executor conf after governor multipliers),
      }

    Doctrine: governor must be HEARD. Hard veto blocks. Soft dissent
    SHAPES (conf × dissent_conf_mult, size × dissent_size_mult) rather
    than killing — unless the effective conf falls below the floor, in
    which case we block to protect against weak-conviction-into-headwind
    trades.
    """
    p = policy or COUNCIL_POLICY["equity"]
    executor_conf = float(
        intent.get("confidence")
        or intent.get("calibrated_confidence")
        or 0.0
    )

    def _result(allowed, reason, disagreement, size_mult, conf_mult, pushback=False):
        """Build the verdict dict, applying the FATAL/SILENCE taxonomy
        (2026-05-18). A non-fatal "block" reason gets downgraded to
        RISK_DOWN_ONLY: allowed=True with a conservative risk multiplier
        instead of zeroing the trade. Only reasons in
        FATAL_GOVERNOR_REASONS stay hard-blocked.

        2026-02-20 audit enrichment (operator directive): every verdict
        carries structured `raw_conf`, `floor`, `governor_mult`, and
        `eff_conf` fields so the post-mortem and downstream audit
        consumers can answer "why did the council block this intent?"
        deterministically without parsing the human reason string.
        Critical for SOFT_DISSENT_BELOW_FLOOR — operators need the
        actual numbers to know whether to tune the floor or accept the
        rejection.
        """
        eff_conf = executor_conf * conf_mult
        floor_val = float(p["MIN_EXECUTOR_CONF_FLOOR"])
        audit_payload = {
            "raw_conf": round(executor_conf, 4),
            "eff_conf": round(eff_conf, 4),
            "floor": round(floor_val, 4),
            "governor_mult": round(conf_mult, 4),
        }

        if not allowed and not governor_blocks_execution(reason):
            # Non-fatal silence/dissent: downgrade to RISK_DOWN_ONLY.
            # The trade can still proceed at reduced size if every
            # other gate passes. Operator sees the cause on the ledger.
            silence_mult = governor_risk_multiplier(reason)
            # If the rule path set size_mult=0 (legacy hard-block
            # semantics), use the silence multiplier as the absolute
            # floor instead of multiplying zero.
            effective_size = silence_mult if size_mult == 0.0 else size_mult * silence_mult
            return {
                "allowed": True,
                "reason": reason,
                "disagreement": disagreement,
                "record_pushback": True,
                "risk_multiplier": _clamp_size(effective_size, p),
                "effective_conf": eff_conf,
                "execution_effect": "RISK_DOWN_ONLY",
                "display_status": "RISK_DOWN",
                "restriction_source": RESTRICTION_SOURCE_GOVERNOR,
                **audit_payload,
            }

        # Either fatal block, or rule said allow.
        return {
            "allowed": allowed,
            "reason": reason,
            "disagreement": disagreement,
            "record_pushback": pushback,
            "risk_multiplier": _clamp_size(size_mult, p) if allowed else 0.0,
            "effective_conf": eff_conf,
            "execution_effect": "ALLOW" if allowed else "HARD_BLOCK",
            "display_status": "ALLOW" if allowed else "BLOCK",
            "restriction_source": (
                RESTRICTION_SOURCE_ROADGUARD if not allowed
                else RESTRICTION_SOURCE_GOVERNOR
            ),
            **audit_payload,
        }

    # Governor seat vacant — no one to be heard. Block.
    if not governor_holder:
        return _result(False, "GOVERNOR_SEAT_VACANT", False, 0.0, 0.0)

    # Governor alive but no stance on this symbol → SOFT downweight, do
    # NOT hard-block. Better to trade smaller than to freeze the action
    # space waiting for a stance that may never come.
    if gov_norm is None:
        if not governor_alive:
            return _result(False, "GOVERNOR_OFFLINE", False, 0.0, 0.0)
        size_mult = p["GOVERNOR_NO_STANCE_SIZE_MULT"]
        conf_mult = p["GOVERNOR_NO_STANCE_CONF_MULT"]
        eff_conf = executor_conf * conf_mult
        if eff_conf < p["MIN_EXECUTOR_CONF_FLOOR"]:
            return _result(False, "NO_STANCE_LOW_EFFECTIVE_CONF", True, 0.0, conf_mult, pushback=True)
        return _result(True, "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT", True, size_mult, conf_mult, pushback=True)

    governor_veto = bool(gov_norm.get("veto", False))
    governor_conf = float(gov_norm.get("confidence") or 0.0)
    executable = gov_norm.get("executable")
    stance = str(gov_norm.get("stance") or "").upper()

    # Disagreement signal — any of these count.
    disagreement = (
        governor_veto
        or executable is False
        or stance in {"VETO", "DISSENT", "RISK_DOWN", "HOLD", "REJECT", "ABSTAIN"}
    )

    # Hard veto: explicit veto bit AND high conviction. True safety stop.
    if governor_veto and governor_conf >= p["GOVERNOR_HARD_VETO_THRESHOLD"]:
        return _result(False, "GOVERNOR_HARD_VETO", True, 0.0, 0.0, pushback=True)

    # Soft dissent: shape, don't kill. conf × dissent_conf_mult; size ×
    # dissent_size_mult. Block only if effective conf falls below floor.
    if disagreement:
        size_mult = p["GOVERNOR_DISSENT_SIZE_MULT"]
        conf_mult = p["GOVERNOR_DISSENT_CONF_MULT"]
        eff_conf = executor_conf * conf_mult
        if eff_conf < p["MIN_EXECUTOR_CONF_FLOOR"]:
            return _result(False, "SOFT_DISSENT_BELOW_FLOOR", True, 0.0, conf_mult, pushback=True)
        return _result(True, "SOFT_DISSENT_DOWNWEIGHTED", True, size_mult, conf_mult, pushback=True)

    # No dissent — lane baseline. Momentum weighting allows crypto to
    # punch slightly above the equity baseline (≤ MAX_UPWEIGHT).
    size_mult = 1.0 * p["MOMENTUM_WEIGHTING"]
    return _result(True, "NO_GOVERNOR_DISSENT", False, size_mult, 1.0)


# ── Refactored helpers (2026-05-17): _evaluate_council was a 334-line
# function. Splitting into the named steps below preserves behavior
# while making each phase independently testable. Sequence:
#   1) resolve governor context  →  _resolve_governor_context()
#   2) compute graduated verdict →  _governance_verdict()  (existing)
#   3) build governor gate row   →  _build_governor_gate()
#   4) resolve opponent gate     →  _evaluate_opponent_gate()
#   5) compose size w/ opponent  →  _compose_size_with_opponent()
#   6) overlay quantum state     →  _apply_quantum_overlay()
#   7) audit + write ledger      →  _persist_council_decision()
#
# Doctrine is unchanged. Locked by tests/test_governance_verdict.py.


async def _resolve_governor_context(
    sym: Optional[str], lane: Optional[str],
) -> tuple[Optional[str], Optional[dict], bool, Optional[str]]:
    """Look up the governor seat holder, their normalized most-recent
    call for `sym`, and whether the seat is alive overall.

    Returns: (governor_holder, gov_norm, governor_alive, gov_any_ts).

    Doctrine pin (2026-02-17, opinion-staleness gate hardening):
        Before this pass, a `gov_norm` found for `sym` set
        `governor_alive = True` unconditionally — meaning a 6h-old
        stance on SPY would keep the governor gate "live" forever.
        After this pass, the stance ITSELF is freshness-checked
        against `_GOVERNOR_OFFLINE_THRESHOLD_SECONDS`. A stale
        stance is treated as `gov_norm = None` + `alive = False`,
        which routes downstream into the GOVERNOR_OFFLINE → hard-
        block path. Closes the "dead governor, cached opinion still
        gates trades" loophole.
    """
    governor_holder, gov_doc = await _latest_governor_call(sym, lane=lane)
    gov_norm = _normalize_governor_call(gov_doc)
    if gov_norm is not None:
        # Freshness gate on the stance itself. If stale → treat as if
        # the governor has no current stance AT ALL on this symbol AND
        # has gone offline. Downstream `_governance_verdict` will hit
        # the `gov_norm is None + not governor_alive` branch and emit
        # `GOVERNOR_OFFLINE` (hard block) — same behavior as if the
        # governor had never opined.
        if not _is_fresh(gov_norm.get("ts"), _GOVERNOR_OFFLINE_THRESHOLD_SECONDS):
            stale_ts = gov_norm.get("ts")
            gov_norm = None
            gov_any_ts = stale_ts
            governor_alive = False
            return governor_holder, gov_norm, governor_alive, gov_any_ts
        # Stance is fresh → proceed as before.
        governor_alive = True
        gov_any_ts = gov_norm.get("ts")
    else:
        _, gov_any = await _latest_governor_any_call(lane=lane)
        governor_alive = _is_fresh(_doc_ts(gov_any), _GOVERNOR_OFFLINE_THRESHOLD_SECONDS)
        gov_any_ts = _doc_ts(gov_any)
    return governor_holder, gov_norm, governor_alive, gov_any_ts


def _build_governor_gate(
    verdict: dict,
    governor_holder: Optional[str],
    executor_holder: Optional[str],
    gov_norm: Optional[dict],
    intent: dict,
    policy: dict,
    lane: Optional[str],
    sym: Optional[str],
    gov_any_ts: Optional[str],
) -> dict:
    """Format the governor verdict into the governor_authority gate row
    the gate chain consumes."""
    gov_reason_text = {
        "GOVERNOR_HARD_VETO": (
            f"GOVERNOR ({governor_holder}) HARD VETO on {sym} ({lane or 'equity'}): "
            f"conf={gov_norm.get('confidence') if gov_norm else 'n/a'} "
            f"≥ {policy['GOVERNOR_HARD_VETO_THRESHOLD']}"
        ),
        "SOFT_DISSENT_DOWNWEIGHTED": (
            f"GOVERNOR ({governor_holder}) dissented on {sym}; "
            f"executor ({executor_holder}) conf "
            f"{float(intent.get('confidence') or 0.0):.2f} × "
            f"{policy['GOVERNOR_DISSENT_CONF_MULT']:.2f} = "
            f"{verdict.get('effective_conf', 0):.2f} (≥ floor "
            f"{policy['MIN_EXECUTOR_CONF_FLOOR']:.2f}) — fires at "
            f"size×{verdict['risk_multiplier']:.2f}"
        ),
        "SOFT_DISSENT_BELOW_FLOOR": (
            f"GOVERNOR ({governor_holder}) dissented on {sym}; "
            f"effective conf {verdict.get('effective_conf', 0):.2f} "
            f"< floor {policy['MIN_EXECUTOR_CONF_FLOOR']:.2f} — "
            f"conviction too weak after governor suppression"
        ),
        "GOVERNOR_NO_STANCE_SOFT_DOWNWEIGHT": (
            f"GOVERNOR ({governor_holder}) live but no stance on {sym} — "
            f"soft downweight: size×{verdict['risk_multiplier']:.2f}"
        ),
        "NO_STANCE_LOW_EFFECTIVE_CONF": (
            f"GOVERNOR ({governor_holder}) silent on {sym} AND executor "
            f"conf too low after suppression "
            f"({verdict.get('effective_conf', 0):.2f} < "
            f"{policy['MIN_EXECUTOR_CONF_FLOOR']:.2f})"
        ),
        "NO_GOVERNOR_DISSENT": (
            f"GOVERNOR ({governor_holder}) recorded stance with no "
            f"dissent on {sym} — size×{verdict['risk_multiplier']:.2f}"
        ),
        "GOVERNOR_OFFLINE": (
            f"GOVERNOR ({governor_holder}) silent for "
            f"≥ {_GOVERNOR_OFFLINE_THRESHOLD_SECONDS // 60}m — "
            f"last seen: {gov_any_ts or 'never'}"
        ),
        "GOVERNOR_SEAT_VACANT": (
            "GOVERNOR seat is vacant — no one to record a stance"
        ),
    }
    return {
        "name": "governor_authority",
        "passed": verdict["allowed"],
        "reason": gov_reason_text.get(verdict["reason"], verdict["reason"]),
        "verdict_code": verdict["reason"],
        "disagreement": verdict["disagreement"],
        "risk_multiplier": verdict["risk_multiplier"],
        "effective_conf": verdict.get("effective_conf"),
        # 2026-02-20 audit enrichment — structured fields surfaced
        # from `_governance_verdict._result`. Lets the post-mortem
        # show the exact math behind a SOFT_DISSENT_BELOW_FLOOR block
        # without parsing the human reason string.
        "raw_conf": verdict.get("raw_conf"),
        "eff_conf": verdict.get("eff_conf"),
        "floor": verdict.get("floor"),
        "governor_mult": verdict.get("governor_mult"),
        "lane": lane,
        "policy_used": "crypto" if (lane or "").lower() == "crypto" else "equity",
    }


def _opponent_payload(opp_doc: dict) -> tuple[float, str]:
    """Extract (confidence, side_lower) from an opponent contribution."""
    payload = (
        opp_doc.get("payload")
        or opp_doc.get("data")
        or opp_doc.get("contribution")
        or {}
    )
    if not isinstance(payload, dict):
        payload = {}
    r_conf = float(
        payload.get("confidence")
        or payload.get("conviction")
        or opp_doc.get("confidence")
        or 0.0
    )
    r_side_raw = (
        payload.get("side")
        or payload.get("stance")
        or payload.get("bias")
        or opp_doc.get("side")
        or ""
    )
    return r_conf, str(r_side_raw).lower()


def _opposes_direction(action: str, r_side: str) -> bool:
    """Does opponent's `r_side` oppose the action direction?"""
    direction = (
        "bullish" if action in ("BUY", "COVER")
        else "bearish" if action in ("SELL", "SHORT")
        else None
    )
    return bool(
        (direction == "bullish" and r_side in ("bearish", "short", "sell", "down"))
        or (direction == "bearish" and r_side in ("bullish", "long", "buy", "up"))
    )


async def _evaluate_opponent_gate(
    action: str, sym: Optional[str], lane: Optional[str],
) -> dict:
    """Build the opponent_objection gate row (advisory; never blocks).
    Seat-bound: reads whoever holds the Opponent seat for this lane."""
    opponent_holder, opp_doc = await _latest_opponent_contribution(lane=lane)
    opp_ts = _doc_ts(opp_doc)

    if not opponent_holder:
        return {
            "name": "opponent_objection",
            "passed": True,
            "reason": "OPPONENT seat vacant — no opposition signal",
            "opponent_holder": None,
            "opponent_conf": 0.0,
            "opponent_side": None,
            "opponent_opposes": False,
        }
    if not opp_doc or not _is_fresh(opp_ts):
        return {
            "name": "opponent_objection",
            "passed": True,
            "reason": f"OPPONENT ({opponent_holder}) silent — no fresh contribution",
            "opponent_holder": opponent_holder,
            "opponent_conf": 0.0,
            "opponent_side": None,
            "opponent_opposes": False,
        }
    r_conf, r_side = _opponent_payload(opp_doc)
    opposes = _opposes_direction(action, r_side)
    return {
        "name": "opponent_objection",
        "passed": True,  # advisory; never blocks on its own
        "reason": (
            f"OPPONENT ({opponent_holder}) {r_side or 'neutral'} "
            f"@ conf {r_conf:.2f} — "
            + ("opposes " if opposes else "agrees with ")
            + f"{action} {sym} (advisory, logged for outcome scoring)"
        ),
        "opponent_holder": opponent_holder,
        "opponent_conf": r_conf,
        "opponent_side": r_side,
        "opponent_opposes": opposes,
    }


def _compose_size_with_opponent(
    verdict: dict,
    opp_gate: dict,
    action: str,
    sym: Optional[str],
    policy: dict,
) -> tuple[float, float]:
    """Apply opponent influence to the council-base size. Returns
    (final_size, opp_influence_applied). Mutates `opp_gate`'s reason
    when influence is applied so the UI surfaces the pull."""
    base_size = verdict["risk_multiplier"]  # already 0 if blocked
    final_size = base_size
    opp_influence_applied = 0.0

    if verdict["allowed"] and opp_gate.get("opponent_opposes"):
        opp_conf = float(opp_gate["opponent_conf"])
        raw_pull = opp_conf * policy["OPPONENT_INFLUENCE"]
        opp_influence_applied = min(raw_pull, policy["MAX_SINGLE_AGENT_INFLUENCE"])
        final_size = _clamp_agent_delta(
            base_size, base_size * (1.0 - opp_influence_applied), policy,
        )
        opp_gate["reason"] = (
            f"OPPONENT ({opp_gate['opponent_holder']}) opposes "
            f"{action} {sym} @ conf {opp_gate['opponent_conf']:.2f} — "
            f"size pulled by {opp_influence_applied:.0%} "
            f"(base {base_size:.2f} → {final_size:.2f})"
        )
        opp_gate["opp_influence_applied"] = opp_influence_applied

    final_size = _clamp_size(final_size, policy) if verdict["allowed"] else 0.0
    return final_size, opp_influence_applied


def _quantum_opinions(
    intent: dict,
    action: str,
    executor_holder: Optional[str],
    gov_norm: Optional[dict],
    governor_holder: Optional[str],
    opp_gate: dict,
) -> list:
    """Build the brain-opinion list the quantum state consumes from the
    council's stances. Lane-neutral, pure."""
    HOLD_STANCES = {"HOLD", "VETO", "DISSENT", "REJECT", "ABSTAIN", "RISK_DOWN"}
    qs_opinions: list = []
    qs_opinions.append(_QSBrainOpinion(
        brain=str(executor_holder or intent.get("stack") or "executor"),
        direction=str(action),
        confidence=float(intent.get("confidence") or 0.0),
    ))
    if gov_norm is not None:
        if (gov_norm.get("veto") or gov_norm.get("executable") is False
                or str(gov_norm.get("stance") or "").upper() in HOLD_STANCES):
            gov_dir = "HOLD"
        elif gov_norm.get("executable") is True:
            gov_dir = action
        else:
            gov_dir = "HOLD"
        qs_opinions.append(_QSBrainOpinion(
            brain=str(governor_holder or "governor"),
            direction=gov_dir,
            confidence=float(gov_norm.get("confidence") or 0.5),
        ))
    opp_side = (opp_gate.get("opponent_side") or "").lower() if opp_gate else ""
    if opp_side:
        opp_dir = (
            "SHORT" if opp_side in ("bearish", "short", "sell", "down")
            else "BUY" if opp_side in ("bullish", "long", "buy", "up")
            else "HOLD"
        )
        qs_opinions.append(_QSBrainOpinion(
            brain=str(opp_gate.get("opponent_holder") or "opponent"),
            direction=opp_dir,
            confidence=float(opp_gate.get("opponent_conf") or 0.0),
        ))
    return qs_opinions


def _apply_quantum_overlay(
    verdict: dict,
    final_size: float,
    intent: dict,
    action: str,
    executor_holder: Optional[str],
    gov_norm: Optional[dict],
    governor_holder: Optional[str],
    opp_gate: dict,
    policy: dict,
) -> tuple[float, dict]:
    """Compose the quantum-inspired regime overlay on top of the
    council-composed size. Quantum may modulate risk only — it cannot
    change direction or promote HOLD into a trade. Returns
    (post_overlay_size, quantum_dict)."""
    qs_opinions = _quantum_opinions(
        intent, action, executor_holder, gov_norm, governor_holder, opp_gate,
    )
    market_features = intent.get("features") or intent.get("market_features") or {}
    qs_verdict = _build_quantum_state(
        opinions=qs_opinions,
        market_features=market_features if isinstance(market_features, dict) else {},
    )
    quantum_dict = qs_verdict.to_dict()
    if verdict["allowed"]:
        pre_qs = final_size
        final_size = _clamp_size(final_size * qs_verdict.risk_multiplier, policy)
        quantum_dict["pre_quantum_size"] = pre_qs
        quantum_dict["post_quantum_size"] = final_size
    return final_size, quantum_dict


def _build_governance_ledger_row(
    intent: dict,
    sym: Optional[str],
    lane: Optional[str],
    action: str,
    intent_id: str,
    executor_holder: Optional[str],
    governor_holder: Optional[str],
    gov_norm: Optional[dict],
    verdict: dict,
    opp_gate: dict,
    opp_influence_applied: float,
    base_size: float,
    final_size: float,
    quantum_dict: dict,
    policy: dict,
    gov_gate: dict,
) -> dict:
    """Compose the per-intent learning ledger row written to
    SHARED_GOVERNANCE_DECISIONS. No DB IO; pure dict builder."""
    exec_personality = _personality_of(executor_holder) or {}
    gov_personality = _personality_of(governor_holder) or {}
    opp_personality = _personality_of(opp_gate.get("opponent_holder")) or {}
    return {
        "ts": _now_iso(),
        "intent_id": intent_id,
        "symbol": sym,
        "lane": lane,
        "policy_used": "crypto" if (lane or "").lower() == "crypto" else "equity",
        "executor_seat_holder": executor_holder,
        "executor_personality_bias": exec_personality.get("bias"),
        "executor_action": action,
        "executor_confidence": float(intent.get("confidence") or 0.0),
        "executor_effective_conf": verdict.get("effective_conf"),
        "governor_seat_holder": governor_holder,
        "governor_personality_bias": gov_personality.get("bias"),
        "governor_stance": (gov_norm or {}).get("stance"),
        "governor_executable": (gov_norm or {}).get("executable"),
        "governor_veto": (gov_norm or {}).get("veto"),
        "governor_confidence": (gov_norm or {}).get("confidence"),
        "governor_call_ts": (gov_norm or {}).get("ts"),
        "opponent_seat_holder": opp_gate.get("opponent_holder"),
        "opponent_personality_bias": opp_personality.get("bias"),
        "opponent_confidence": opp_gate.get("opponent_conf"),
        "opponent_side": opp_gate.get("opponent_side"),
        "opponent_opposes": opp_gate.get("opponent_opposes"),
        "opp_influence_applied": opp_influence_applied,
        "disagreement": verdict["disagreement"],
        "verdict_code": verdict["reason"],
        "final_allowed": verdict["allowed"],
        "base_risk_multiplier": base_size,
        "risk_multiplier": final_size,
        "quantum_state": quantum_dict,
        "hard_limits_respected": gov_gate.get("hard_limits_respected", True)
        and opp_gate.get("hard_limits_respected", True),
        "stack_weights": policy["STACK_WEIGHTS"],
        "thresholds": {
            "hard_veto":          policy["GOVERNOR_HARD_VETO_THRESHOLD"],
            "dissent_conf_mult":  policy["GOVERNOR_DISSENT_CONF_MULT"],
            "dissent_size_mult":  policy["GOVERNOR_DISSENT_SIZE_MULT"],
            "min_executor_conf_floor": policy["MIN_EXECUTOR_CONF_FLOOR"],
            "opponent_influence": policy["OPPONENT_INFLUENCE"],
            "max_upweight":       policy["MAX_UPWEIGHT"],
            "max_downweight":     policy["MAX_DOWNWEIGHT"],
            "max_agent_influence": policy["MAX_SINGLE_AGENT_INFLUENCE"],
            "momentum_weighting": policy["MOMENTUM_WEIGHTING"],
        },
    }


def _audit_council_to_shelly(
    intent: dict, gov_gate: dict, opp_gate: dict,
    intent_id: str, sym: Optional[str], action: str,
) -> None:
    """Write both gate decisions to mc_shelly for training."""
    for g in (gov_gate, opp_gate):
        record_async(
            event_type="council_pass" if g["passed"] else "council_block",
            brain=intent.get("stack"),
            symbol=sym,
            action=action,
            outcome="pass" if g["passed"] else "block",
            rationale=g["reason"],
            ref_id=intent_id,
            gate_name=g["name"],
        )


async def _evaluate_council(intent: dict) -> tuple[list[dict], float]:
    """Returns (gate_rows, risk_multiplier).

    Orchestrator: runs the council pipeline end-to-end. Each phase is a
    small helper defined above; this function's only job is composition
    + persistence. Doctrine is locked by tests/test_governance_verdict.py.
    """
    sym = intent.get("symbol")
    action = (intent.get("action") or "").upper()
    intent_id = intent.get("intent_id", "?")
    lane = intent.get("lane")
    policy = _policy_for_lane(lane)

    executor_holder = await _seat_holder("executor", lane=lane)
    (governor_holder, gov_norm, governor_alive, gov_any_ts) = await _resolve_governor_context(sym, lane)

    # Phase 1: graduated verdict from the (pure) verdict function.
    verdict = _governance_verdict(intent, gov_norm, governor_alive, governor_holder, policy)

    # Phase 2: format the governor gate row.
    gov_gate = _build_governor_gate(
        verdict, governor_holder, executor_holder, gov_norm, intent,
        policy, lane, sym, gov_any_ts,
    )

    # Phase 3: opponent gate (advisory).
    opp_gate = await _evaluate_opponent_gate(action, sym, lane)

    # Phase 4: compose size with opponent influence.
    base_size = verdict["risk_multiplier"]
    final_size, opp_influence_applied = _compose_size_with_opponent(
        verdict, opp_gate, action, sym, policy,
    )

    # Phase 5: quantum-inspired regime overlay (risk modulation only).
    final_size, quantum_dict = _apply_quantum_overlay(
        verdict, final_size, intent, action, executor_holder, gov_norm,
        governor_holder, opp_gate, policy,
    )
    gov_gate["risk_multiplier"] = final_size
    gov_gate["quantum_state"] = quantum_dict

    # Phase 6: audit to shelly.
    _audit_council_to_shelly(intent, gov_gate, opp_gate, intent_id, sym, action)

    # Phase 7: stamp advisory personality envelopes on gate rows.
    if governor_holder:
        _stamp_personality(governor_holder, gov_gate)
    if opp_gate.get("opponent_holder"):
        _stamp_personality(opp_gate["opponent_holder"], opp_gate)

    # Phase 8: write the governance ledger row (best-effort).
    governance_row = _build_governance_ledger_row(
        intent, sym, lane, action, intent_id, executor_holder,
        governor_holder, gov_norm, verdict, opp_gate, opp_influence_applied,
        base_size, final_size, quantum_dict, policy, gov_gate,
    )
    try:
        await db[SHARED_GOVERNANCE_DECISIONS].insert_one(governance_row)
    except Exception:  # noqa: BLE001
        # The governance ledger is for learning; don't let a write
        # failure kill the gate evaluation.
        pass

    return [gov_gate, opp_gate], final_size

