"""
Mythos Defense Layer — Security Constants
==========================================

All policy constants used by the Mythos Defense Layer.
Centralised here so policy changes require a single-file edit
with a clear audit trail.

Deployment phases
-----------------
OBSERVE   Phase 1 — log only; no intent is blocked. Use to baseline
                     false-positive rate before enabling enforcement.
BLOCK     Phase 2 — block hard violations (fake brain, unsigned external
                     source, secret exfiltration, direct broker attempt,
                     memory poisoning, replay intent).
LOCKDOWN  Phase 3 — all of Phase 2 plus auto-submit and broker connections
                     are disabled system-wide.

The active phase is controlled by the MYTHOS_DEPLOY_PHASE environment
variable ("OBSERVE" | "BLOCK" | "LOCKDOWN").  Defaults to "OBSERVE" so
that the first deployment is always safe.
"""

import os
from enum import Enum


# ---------------------------------------------------------------------------
# Deployment Phase
# ---------------------------------------------------------------------------

class DeployPhase(str, Enum):
    OBSERVE  = "OBSERVE"    # Log only — no blocking
    BLOCK    = "BLOCK"      # Block hard security violations
    LOCKDOWN = "LOCKDOWN"   # Block + disable auto-submit / broker connections


ACTIVE_DEPLOY_PHASE: DeployPhase = DeployPhase(
    os.environ.get("MYTHOS_DEPLOY_PHASE", DeployPhase.OBSERVE).upper()
)


# ---------------------------------------------------------------------------
# Severity Levels
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    WARN     = "warn"       # Allow intent, stamp receipt with warning
    BLOCK    = "block"      # Stop intent; do not forward to Seat
    LOCKDOWN = "lockdown"   # Stop intent; additionally invoke LOCKDOWN_CONTRACT


# ---------------------------------------------------------------------------
# LOCKDOWN Contract
# ---------------------------------------------------------------------------
# Defines exactly which system components are disabled when a LOCKDOWN
# severity violation is detected in LOCKDOWN phase.
#
# Components that ARE disabled:
#   auto_submit          — automatic order submission is turned off
#   broker_adapters      — all broker adapter connections are suspended
#   new_broker_sessions  — no new broker sessions may be opened
#
# Components that remain RUNNING (not affected by lockdown):
#   brains               — Brain/Agent processes continue to emit intents
#   research             — Research pipelines continue to run
#   receipts             — Receipt panel and audit log continue to write
#   seat_policy          — Seat continues to evaluate intents (they are
#                          blocked before reaching Seat, but Seat itself
#                          is not shut down)
#
# The lockdown state is signalled via lockdown_triggered=True on the
# receipt.  Downstream consumers (broker adapters, auto-submit scheduler)
# must observe this field and act accordingly.

LOCKDOWN_CONTRACT: dict = {
    "disabled": [
        "auto_submit",
        "broker_adapters",
        "new_broker_sessions",
    ],
    "still_running": [
        "brains",
        "research",
        "receipts",
        "seat_policy",
    ],
}

# ---------------------------------------------------------------------------
# Valid Pipeline Lanes
# ---------------------------------------------------------------------------
# Updated to match RISEDUAL's live lane configuration.

VALID_LANES: frozenset[str] = frozenset({"equity", "crypto"})


# ---------------------------------------------------------------------------
# Valid Trading Actions
# ---------------------------------------------------------------------------

VALID_ACTIONS: frozenset[str] = frozenset({"BUY", "SELL", "HOLD", "ABSTAIN"})


# ---------------------------------------------------------------------------
# Trusted In-Process Runtime Origin
# ---------------------------------------------------------------------------
# When an intent carries runtime_origin == IN_PROCESS_ORIGIN, the
# signed_source requirement is waived.  This supports in-process runners
# that do not attach a runtime token.

IN_PROCESS_ORIGIN: str = "in_process"


# ---------------------------------------------------------------------------
# Targeted High-Risk Fields for Pattern Scanning
# ---------------------------------------------------------------------------
# Scanning is scoped to these fields only to avoid false positives from
# research/news text that may legitimately contain words like "ignore" or
# "export" in a financial context.

HIGH_RISK_FIELDS: tuple[str, ...] = (
    "metadata",
    "reasoning",
    "memory_write",
    "tool_payload",
    "broker_directive",
    "freeform_notes",
)


# ---------------------------------------------------------------------------
# Prompt Injection & Malicious Instruction Patterns
# ---------------------------------------------------------------------------
# Matched case-insensitively against HIGH_RISK_FIELDS only.

BANNED_PATTERNS: list[str] = [
    # Classic prompt injection
    "ignore previous instructions",
    "disregard all prior",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "override safety",

    # Credential exfiltration
    "send credentials",
    "export .env",
    "print api key",
    "reveal secret",
    "dump secrets",
    "leak credentials",
    "exfiltrate",

    # Safety bypass
    "disable roadguard",
    "turn off safety",
    "bypass security",
    "skip validation",
    "disable mythos",
    "remove restrictions",

    # Unauthorized broker action
    "submit order directly",
    "place order without approval",
    "force execute",
    "execute immediately without",

    # Agent impersonation
    "i am the governor",
    "acting as roadguard",
    "pretend you are brain",
    "simulate seat",

    # Memory / research poisoning
    "overwrite memory",
    "corrupt research",
    "invalidate evidence",
    "replace all memories",
]


# ---------------------------------------------------------------------------
# Secret Pattern Fragments
# ---------------------------------------------------------------------------
# Checked against HIGH_RISK_FIELDS string values only.

SECRET_FRAGMENTS: list[str] = [
    "sk-",
    "password=",
    "api_key=",
    "secret=",
    "token=",
    "bearer ",
    "authorization:",
    "private_key",
    "-----begin",
]


# ---------------------------------------------------------------------------
# Research Staleness Threshold (seconds)
# ---------------------------------------------------------------------------

RESEARCH_MAX_AGE_SECONDS: int = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Memory Write Suspicious Keys
# ---------------------------------------------------------------------------

SUSPICIOUS_MEMORY_KEYS: list[str] = [
    "override",
    "root",
    "admin",
    "system",
    "credentials",
    "password",
    "secret",
    "disable",
    "bypass",
]


# ---------------------------------------------------------------------------
# Reason Codes
# ---------------------------------------------------------------------------

class MythosReason:
    # Clean pass
    CLEAR                       = "MYTHOS_CLEAR"

    # Security violations (always block regardless of phase)
    PROMPT_INJECTION            = "MYTHOS_PROMPT_INJECTION"
    DIRECT_BROKER_CONTROL       = "MYTHOS_DIRECT_BROKER_CONTROL_ATTEMPT"
    MISSING_BRAIN_IDENTITY      = "MYTHOS_MISSING_BRAIN_IDENTITY"
    UNSIGNED_RUNTIME_SOURCE     = "MYTHOS_UNSIGNED_RUNTIME_SOURCE"
    SECRETS_IN_PAYLOAD          = "MYTHOS_SECRETS_IN_PAYLOAD"
    SUSPICIOUS_MEMORY_WRITE     = "MYTHOS_SUSPICIOUS_MEMORY_WRITE"
    HIDDEN_TOOL_INSTRUCTION     = "MYTHOS_HIDDEN_TOOL_INSTRUCTION"
    AGENT_IMPERSONATION         = "MYTHOS_AGENT_IMPERSONATION"
    STALE_RESEARCH              = "MYTHOS_STALE_RESEARCH_EVIDENCE"

    # Structural violations (always block)
    INVALID_ACTION              = "MYTHOS_INVALID_ACTION"
    INVALID_LANE                = "MYTHOS_INVALID_LANE"

    # Observe-mode stamp (warn only)
    OBSERVE_MODE_STAMP          = "MYTHOS_OBSERVE_MODE_ACTIVE"

    # Internal error (always block)
    INTERNAL_ERROR              = "MYTHOS_INTERNAL_ERROR"
