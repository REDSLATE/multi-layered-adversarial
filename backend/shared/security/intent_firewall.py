"""
Intent Firewall (v3 — context-aware)
=====================================

Internal security governor for RISEDUAL. v3 of the design imported
from `risedual_mythos_defense_v2_1` (operator-authored, 2026-06-22)
with the prompt-injection scanner upgraded from flat substring
matching to the tiered, boundary-aware matcher in
`firewall_patterns.scan()`.

Architecture position::

    Brain / Agent
        │
        ▼
    Intent Firewall   ◄── THIS MODULE
        │
        ▼
    Seat Policy
        │
        ▼
    Trade Governor
        │
        ▼
    RoadGuard
        │
        ▼
    Broker

Every intent emitted by a Brain or Agent MUST pass through
``intent_firewall_check()`` before it is forwarded to Seat.

Hard rule
---------
The Firewall can BLOCK security violations only:
    - fake brain identity
    - unsigned external source
    - secret exfiltration
    - direct broker control attempt
    - memory poisoning
    - replay / stale research
    - hidden tool instructions
    - prompt injection

It does NOT block for:
    - weak confidence
    - bad spread
    - poor setup quality
    - any trading logic concern

Severity model
--------------
    WARN     → intent is allowed; receipt is stamped with a warning.
    BLOCK    → intent is stopped; Seat never sees it.
    LOCKDOWN → intent is stopped; auto-submit and broker connections
               are additionally disabled system-wide.

Deployment phases
-----------------
    OBSERVE  → no blocking; all intents are stamped and logged only.
    BLOCK    → WARN and BLOCK severities are enforced.
    LOCKDOWN → WARN, BLOCK, and LOCKDOWN severities are enforced.

Default phase is OBSERVE — set `MYTHOS_DEPLOY_PHASE=BLOCK` only after
baselining the false-positive rate from OBSERVE logs.

Prompt-injection scanning (the v3 change vs v2.1)
-------------------------------------------------
v2.1 used flat substring matching against `BANNED_PATTERNS` on
HIGH_RISK_FIELDS. That false-positive'd on legitimate brain
reasoning that quoted news headlines like "You are now seeing
the Fed pivot...". v3 routes through
`firewall_patterns.scan(text, field_kind)`:

    * `reasoning`, `metadata`, `freeform_notes` → FREEFORM —
      weak patterns require sentence-boundary anchor.
    * `tool_payload`, `memory_write`, `broker_directive` →
      STRUCTURED — substring match (these fields don't contain
      legitimate prose, so any banned vocabulary IS suspicious).

See `firewall_patterns.py` for the tiered pattern doctrine.

This module is currently UNWIRED — the pipeline integration is
queued for a dedicated session. `intent_firewall_check(intent)`
can be called directly from tests/scripts to validate behaviour
against the v3 spec.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    SECRET_FRAGMENTS,
    VALID_LANES,
    VALID_ACTIONS,
    RESEARCH_MAX_AGE_SECONDS,
    SUSPICIOUS_MEMORY_KEYS,
    HIGH_RISK_FIELDS,
    IN_PROCESS_ORIGIN,
    ACTIVE_DEPLOY_PHASE,
    LOCKDOWN_CONTRACT,
    DeployPhase,
    Severity,
    MythosReason,
)
from .firewall_patterns import FieldKind, scan as scan_patterns


# v3 change: field-kind taxonomy. Free-form fields apply boundary
# constraints on WEAK patterns; structured fields use raw substring
# match. Driven by `firewall_patterns.FieldKind`.
_FIELD_KIND: dict[str, FieldKind] = {
    "metadata":         FieldKind.FREEFORM,
    "reasoning":        FieldKind.FREEFORM,
    "freeform_notes":   FieldKind.FREEFORM,
    "memory_write":     FieldKind.STRUCTURED,
    "tool_payload":     FieldKind.STRUCTURED,
    "broker_directive": FieldKind.STRUCTURED,
}


_HIDDEN_TOOL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<\s*tool[_\s]", re.IGNORECASE),
    re.compile(r"\[\s*TOOL\s*\]", re.IGNORECASE),
    re.compile(r"\{\{\s*tool", re.IGNORECASE),
    re.compile(r"```\s*(bash|sh|python|js|javascript|tool)", re.IGNORECASE),
    re.compile(r"<\s*function_call\s*>", re.IGNORECASE),
    re.compile(r"<\s*invoke\s+name=", re.IGNORECASE),
]


# ── Receipts ──────────────────────────────────────────────────────


def _make_receipt(
    allowed: bool,
    reason: str,
    severity: Severity,
    intent: Optional[Dict[str, Any]] = None,
    lockdown_triggered: bool = False,
    would_have_severity: Optional[Severity] = None,
    lockdown_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct a stamped Intent Firewall receipt.

    NOTE (2026-06-22): the v2.1 import called `write_receipt(...)`
    here to dual-write to Mongo + NDJSON. That wiring is queued for
    the dedicated Firewall integration session (operator-pinned
    rollout: OBSERVE → BLOCK → LOCKDOWN with per-stage validation).
    For now the receipt is returned to the caller only — tests and
    diagnostic scripts can inspect it without producing audit-row
    side effects.
    """
    return {
        "allowed": allowed,
        "reason": reason,
        "severity": severity.value,
        "security_multiplier": 1.0 if allowed else 0.0,
        "restriction_source": "security",
        "security_layer": "intent_firewall",
        "broker_called": False,
        "lockdown_triggered": lockdown_triggered,
        "deploy_phase": ACTIVE_DEPLOY_PHASE.value,
        "would_have_severity": (
            would_have_severity.value if would_have_severity else None
        ),
        "lockdown_contract": lockdown_contract,
    }


def _block(
    reason: str,
    severity: Severity = Severity.BLOCK,
    intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Produce a blocking receipt — OBSERVE downgrades to WARN
    while preserving `would_have_severity`."""
    if ACTIVE_DEPLOY_PHASE == DeployPhase.OBSERVE:
        return _make_receipt(
            allowed=True,
            reason=f"{MythosReason.OBSERVE_MODE_STAMP}|WOULD_BLOCK:{reason}",
            severity=Severity.WARN,
            intent=intent,
            would_have_severity=severity,
        )
    lockdown = (
        severity == Severity.LOCKDOWN
        and ACTIVE_DEPLOY_PHASE == DeployPhase.LOCKDOWN
    )
    return _make_receipt(
        allowed=False,
        reason=reason,
        severity=severity,
        intent=intent,
        lockdown_triggered=lockdown,
        lockdown_contract=LOCKDOWN_CONTRACT if lockdown else None,
    )


def _warn(reason: str, intent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _make_receipt(
        allowed=True, reason=reason, severity=Severity.WARN, intent=intent,
    )


def _clear(intent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _make_receipt(
        allowed=True,
        reason=MythosReason.CLEAR,
        severity=Severity.WARN,
        intent=intent,
    )


# ── Field extraction (preserves field name for kind routing) ──────


def _extract_typed_field_strings(
    intent: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """Yield `(field_name, string)` tuples for every string under
    HIGH_RISK_FIELDS, preserving the originating field so the
    matcher can apply FREEFORM vs STRUCTURED policy correctly.

    v2.1 returned `List[str]` (lost the field name). v3 needs the
    name to route to the right FieldKind.
    """
    out: List[Tuple[str, str]] = []
    for field in HIGH_RISK_FIELDS:
        value = intent.get(field)
        if value is None:
            continue
        for s in _deep_strings(value):
            out.append((field, s))
    return out


def _deep_strings(obj: Any, depth: int = 0) -> List[str]:
    if depth > 8:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        out: List[str] = []
        for v in obj.values():
            out.extend(_deep_strings(v, depth + 1))
        return out
    if isinstance(obj, (list, tuple)):
        out = []
        for item in obj:
            out.extend(_deep_strings(item, depth + 1))
        return out
    return []


def _extract_targeted_strings(intent: Dict[str, Any]) -> List[str]:
    """v2.1-compatible flat extraction used by the secret-fragment
    scan (which doesn't need field-kind routing)."""
    return [s for _f, s in _extract_typed_field_strings(intent)]


# ── Individual checks ─────────────────────────────────────────────


def _check_fake_brain_identity(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not intent.get("brain_id") or not str(intent.get("brain_id")).strip():
        return _block(MythosReason.MISSING_BRAIN_IDENTITY, Severity.BLOCK, intent)
    return None


def _check_signed_source(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if intent.get("runtime_origin") == IN_PROCESS_ORIGIN:
        return None
    if not intent.get("signed_source") or not str(intent.get("signed_source")).strip():
        return _block(MythosReason.UNSIGNED_RUNTIME_SOURCE, Severity.BLOCK, intent)
    return None


def _check_broker_directive(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if intent.get("broker_directive") is not None:
        return _block(MythosReason.DIRECT_BROKER_CONTROL, Severity.LOCKDOWN, intent)
    return None


def _check_secrets_in_payload(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for s in _extract_targeted_strings(intent):
        s_lower = s.lower()
        for fragment in SECRET_FRAGMENTS:
            if fragment in s_lower:
                return _block(MythosReason.SECRETS_IN_PAYLOAD, Severity.LOCKDOWN, intent)
    return None


def _check_prompt_injection(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """v3 — context-aware prompt-injection scan.

    Routes each (field, string) pair through `firewall_patterns.scan`
    with the field's FieldKind. Legitimate brain reasoning that
    quotes attacker vocabulary in mid-sentence (e.g. a news article
    saying "you are now seeing...") no longer false-positives —
    boundary anchoring on FREEFORM fields requires the WEAK pattern
    to appear at a sentence start.

    STRUCTURED fields (tool_payload, memory_write, broker_directive)
    still use substring match because they should NEVER contain
    natural prose — any banned vocabulary inside them is suspicious
    by definition.

    Compound rule: a single weak match in free-form prose is a
    risk-score bump (WARN), not a BLOCK. Two weak matches in the
    same field, or one weak + one action modifier, escalate to
    BLOCK via the matcher's `compound_*` block_reason.
    """
    aggregate_risk = 0
    aggregate_matches: list[str] = []
    for field, s in _extract_typed_field_strings(intent):
        kind = _FIELD_KIND.get(field, FieldKind.FREEFORM)
        result = scan_patterns(s, kind)
        if result.blocked:
            # Hard block — surface the originating field + pattern
            # so the audit row tells operators exactly where the
            # injection landed.
            return _block(
                f"{MythosReason.PROMPT_INJECTION}:"
                f"{result.block_reason}@{field}",
                Severity.BLOCK,
                intent,
            )
        aggregate_risk += result.risk_score
        aggregate_matches.extend(result.matches)

    # Single-pattern weak matches don't block — they accumulate risk
    # across fields. If the cross-field aggregate climbs above a
    # threshold (60 = same as a single STRONG match's risk_delta),
    # warn rather than silently letting the intent through.
    if aggregate_risk >= 60:
        return _warn(
            f"{MythosReason.PROMPT_INJECTION}:weak_compound:"
            f"score={aggregate_risk}:matches={','.join(aggregate_matches)}",
            intent,
        )
    return None


def _check_hidden_tool_instructions(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for s in _extract_targeted_strings(intent):
        for pat in _HIDDEN_TOOL_PATTERNS:
            if pat.search(s):
                return _block(MythosReason.HIDDEN_TOOL_INSTRUCTION, Severity.BLOCK, intent)
    return None


def _check_memory_poisoning(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mem = intent.get("memory_write")
    if isinstance(mem, dict):
        for key in mem:
            key_lower = str(key).lower()
            for suspicious in SUSPICIOUS_MEMORY_KEYS:
                if suspicious in key_lower:
                    return _block(MythosReason.SUSPICIOUS_MEMORY_WRITE, Severity.BLOCK, intent)
    return None


def _check_replay_research(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ts_raw = intent.get("research_ts")
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > RESEARCH_MAX_AGE_SECONDS:
            return _warn(MythosReason.STALE_RESEARCH, intent)
    except (ValueError, TypeError):
        # Unparseable timestamp is not a security violation in v3.
        # (v2.1 was silently swallowing this — preserved here.)
        return None
    return None


def _check_valid_action(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if intent.get("action") not in VALID_ACTIONS:
        return _block(MythosReason.INVALID_ACTION, Severity.BLOCK, intent)
    return None


def _check_valid_lane(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if intent.get("lane") not in VALID_LANES:
        return _block(MythosReason.INVALID_LANE, Severity.BLOCK, intent)
    return None


# Ordering: most critical first. A single failure short-circuits.
_SECURITY_CHECKS = [
    _check_fake_brain_identity,
    _check_signed_source,
    _check_broker_directive,
    _check_secrets_in_payload,
    _check_prompt_injection,
    _check_hidden_tool_instructions,
    _check_memory_poisoning,
    _check_replay_research,
    _check_valid_action,
    _check_valid_lane,
]


def intent_firewall_check(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Run all Intent Firewall checks against an intent. Returns the
    stamped receipt. Never calls the broker, never modifies the
    intent, never decides BUY/SELL/HOLD.
    """
    try:
        for check in _SECURITY_CHECKS:
            result = check(intent)
            if result is None:
                continue
            if not result.get("allowed", True):
                return result
            # WARN: continue scanning for harder blocks downstream.
            if result.get("severity") == Severity.WARN.value:
                warn_receipt = result
                idx = _SECURITY_CHECKS.index(check)
                for later_check in _SECURITY_CHECKS[idx + 1:]:
                    later = later_check(intent)
                    if later is not None and not later.get("allowed", True):
                        return later
                return warn_receipt
        return _clear(intent)
    except Exception as exc:  # pylint: disable=broad-except
        return _block(
            f"{MythosReason.INTERNAL_ERROR}:{type(exc).__name__}",
            Severity.BLOCK,
            intent,
        )
