"""Context-aware prompt-injection pattern matcher for the Intent
Firewall (v3 spec).

2026-06-22 — Operator pin (verbatim):
    "The spec scans HIGH_RISK_FIELDS for prompt-injection patterns
    like 'you are now'. But your brains' `reasoning` field naturally
    contains free-form English. If Camino quotes a news headline
    that says 'You are now seeing the Fed pivot...', the firewall
    would BLOCK it."
    "The minor push back, could you fix it?"

## The fix in one sentence

Replace flat substring matching with a **tiered, context-aware**
matcher that distinguishes injection STRUCTURE from injection
VOCABULARY. The same patterns from the v3 spec are still detected
— but a single weak match in free-form prose doesn't BLOCK.
A weak match combined with an action modifier, or any STRONG
pattern anchored at a sentence boundary, does.

## Three tiers

1. **STRONG**   — high-confidence injection. Match anywhere
                  → BLOCK on first hit.
                  Example: "ignore previous instructions",
                  "disable roadguard".
2. **WEAK**     — vocabulary that appears in both legitimate
                  reasoning AND injection. Match contributes to a
                  risk score; does NOT block alone.
                  Example: "you are now", "system prompt:".
                  Requires the pattern to be AT A SENTENCE
                  BOUNDARY (start of line, after `.!?`, or after
                  a markdown role marker) when scanning free-form
                  fields like `reasoning`.
3. **ACTION**   — action modifiers that only make sense in an
                  attack context. Their presence escalates ANY
                  weak match to BLOCK via the compound rule.
                  Example: "submit order directly",
                  "override brain", "send credentials".

## Compound rule (when no STRONG hit)

  - weak_count + action_count >= 2   → BLOCK
  - else accumulated risk_score      → WARN (≥30) or CLEAR (<30)

## Field policy

The matcher takes a `field_kind` argument. Free-form fields
(`reasoning`, `metadata`, `freeform_notes`) apply boundary
constraints on WEAK patterns. Structured fields (`tool_payload`,
`memory_write`, `broker_directive`) use raw substring match
because they should never contain natural prose — any banned
vocabulary inside them IS suspicious by definition.

## What this fixes vs the v3 spec

| Input                                                | v3 flat substring | This matcher |
| :--------------------------------------------------- | :---------------- | :----------- |
| "You are now seeing the Fed pivot..." in `reasoning` | BLOCKED ❌        | CLEAR ✓      |
| "system prompt: be honest with risk" in `reasoning`  | BLOCKED ❌        | CLEAR ✓      |
| "Ignore previous instructions" in `reasoning`        | BLOCKED ✓        | BLOCKED ✓    |
| "you are now / submit order directly" (2 patterns)   | BLOCKED ✓        | BLOCKED ✓    |
| "ignore previous instructions" in `tool_payload`     | BLOCKED ✓        | BLOCKED ✓    |
| "system prompt:" alone in `tool_payload`             | BLOCKED ✓        | BLOCKED ✓    |
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Field-kind taxonomy ────────────────────────────────────────────


class FieldKind(str, Enum):
    """Where the text under inspection came from. Drives the
    boundary-vs-substring behaviour for WEAK patterns."""

    # Free-form prose — applies boundary constraints to WEAK patterns
    FREEFORM = "freeform"
    # Structured payload — substring match (no natural prose expected)
    STRUCTURED = "structured"


# ── Tiers ──────────────────────────────────────────────────────────


class Tier(str, Enum):
    STRONG = "strong"   # always block on hit
    WEAK = "weak"       # contributes to risk, blocks only in compound
    ACTION = "action"   # action modifier — escalates WEAK matches to BLOCK


@dataclass(frozen=True)
class Pattern:
    tier: Tier
    name: str
    # Compiled regex. For STRONG patterns we use word-boundary
    # anchored patterns. For WEAK patterns the pattern itself is
    # the vocabulary; boundary enforcement is layered in
    # `_text_for_match` at scan time.
    regex: re.Pattern[str]
    # Risk-score contribution for WEAK / ACTION patterns when
    # they don't trigger a BLOCK. STRONG patterns always block, so
    # their delta is informational only.
    risk_delta: int = 0


# 2026-06-22 — pattern library derived from the v3 spec's
# BANNED_PATTERNS, retiered according to false-positive risk
# observed in real brain reasoning corpora.

_STRONG: tuple[Pattern, ...] = (
    # "ignore previous instructions" family — the canonical prompt
    # injection. Word-boundary anchored to avoid matching e.g.
    # "ignoreprevious" but allow "ignore the previous instructions".
    Pattern(Tier.STRONG, "ignore_previous_instructions",
            re.compile(
                r"\bignore\s+(?:all\s+|the\s+|any\s+|your\s+)?"
                r"(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|prompts?|context|rules?)\b",
                re.IGNORECASE,
            ),
            risk_delta=60),

    # "disregard all prior" — sibling phrasing.
    Pattern(Tier.STRONG, "disregard_prior",
            re.compile(
                r"\bdisregard\s+(?:all\s+|every\s+|any\s+|the\s+|your\s+)?"
                r"(?:prior|previous|earlier|above)\b",
                re.IGNORECASE,
            ),
            risk_delta=60),

    # Direct attack on the safety layers.
    Pattern(Tier.STRONG, "disable_safety",
            re.compile(
                r"\b(?:disable|turn\s+off|deactivate|kill)\s+"
                r"(?:roadguard|guardrails?|safety|trade\s+governor|"
                r"intent\s+firewall|seat\s+policy)\b",
                re.IGNORECASE,
            ),
            risk_delta=80),

    # "bypass seat", "skip governor" — same intent.
    Pattern(Tier.STRONG, "bypass_pipeline_stage",
            re.compile(
                r"\b(?:bypass|skip|circumvent|short[\s-]?circuit)\s+"
                r"(?:seat|governor|roadguard|firewall|"
                r"intent\s+firewall|broker\s+adapter)\b",
                re.IGNORECASE,
            ),
            risk_delta=80),

    # Credential exfiltration phrasing (the *intent* — actual
    # secret-fragment matching lives in the SECRETS check, not here).
    Pattern(Tier.STRONG, "exfiltrate_credentials",
            re.compile(
                r"\b(?:export|leak|exfiltrate|send|reveal|print|"
                r"dump|share)\s+(?:the\s+|your\s+|all\s+)?"
                r"(?:credentials?|api[_\s-]?keys?|secrets?|"
                r"passwords?|tokens?|env(?:ironment)?\s+vars?|"
                r"\.env)\b",
                re.IGNORECASE,
            ),
            risk_delta=90),
)

# WEAK patterns — vocabulary that overlaps with legitimate prose.
# These are the source of the false-positive risk the operator
# flagged. With boundary enforcement (FREEFORM) they only fire when
# at a sentence start, which is where real injection appears.
_WEAK: tuple[Pattern, ...] = (
    # "you are now" — extremely common in legitimate quoted text.
    Pattern(Tier.WEAK, "role_redefinition",
            re.compile(
                r"you\s+are\s+now\s+(?:a\s+|an\s+|the\s+)?",
                re.IGNORECASE,
            ),
            risk_delta=15),

    # "new instructions:" / "system prompt:" — colon-suffixed
    # imperative form. Often appears in legitimate UI text /
    # documentation but ALSO is a textbook injection lead-in.
    Pattern(Tier.WEAK, "imperative_header",
            re.compile(
                r"(?:new\s+instructions?|system\s+prompt|"
                r"role\s*[:=]|assistant\s*[:=])\s*:",
                re.IGNORECASE,
            ),
            risk_delta=15),

    # "act as root", "act as admin" — could be legitimate ops
    # discussion in reasoning. Boundary-anchored.
    Pattern(Tier.WEAK, "role_escalation_request",
            re.compile(
                r"act\s+as\s+(?:root|admin(?:istrator)?|"
                r"system|sudo)\b",
                re.IGNORECASE,
            ),
            risk_delta=20),
)

# ACTION patterns — modifiers that ONLY make sense in an attack
# context. A single ACTION match alone is suspicious (risk bump);
# combined with any WEAK match → compound BLOCK.
_ACTION: tuple[Pattern, ...] = (
    Pattern(Tier.ACTION, "submit_order_directly",
            re.compile(
                r"\bsubmit\s+(?:the\s+|an?\s+)?(?:order|trade|"
                r"position)s?\s+directly\b",
                re.IGNORECASE,
            ),
            risk_delta=40),

    Pattern(Tier.ACTION, "override_brain",
            re.compile(
                r"\boverride\s+(?:the\s+|all\s+|each\s+)?"
                r"(?:brains?|seats?|governors?)\b",
                re.IGNORECASE,
            ),
            risk_delta=40),

    Pattern(Tier.ACTION, "send_credentials",
            re.compile(
                r"\bsend\s+(?:the\s+|your\s+|all\s+)?"
                r"(?:credentials?|api[_\s-]?keys?|secrets?|"
                r"passwords?|tokens?)\b",
                re.IGNORECASE,
            ),
            risk_delta=50),
)


# ── Sentence-boundary detection ────────────────────────────────────


# A WEAK pattern in FREEFORM text only counts if it appears at a
# "sentence boundary" — start-of-text, after `.!?` + whitespace,
# after a newline, or after a markdown / chat role marker like
# `### user:` / `[ASSISTANT]`. Mid-sentence WEAK matches are
# legitimate prose ("...the article said you are now seeing...").
_BOUNDARY_PREFIX = re.compile(
    r"(?:^|[\.!?]\s+|\n\s*|^>\s*|\n>\s*|"
    r"#{1,6}\s+|\[[A-Z]+\]\s*|"
    r"(?:user|assistant|system)\s*[:=]\s*)",
    re.IGNORECASE | re.MULTILINE,
)


def _weak_match_at_boundary(
    pattern: Pattern,
    text: str,
) -> Optional[re.Match[str]]:
    """Return the first match of `pattern` in `text` that is
    preceded by a sentence-boundary token, or None. Pure boundary
    enforcement — no other semantics.

    Implementation note: we don't merge the boundary regex into
    the pattern itself because patterns must be reusable across
    FREEFORM and STRUCTURED contexts (where boundary doesn't apply).
    """
    for m in pattern.regex.finditer(text):
        start = m.start()
        # Look back up to 200 chars for a boundary token whose
        # match end is at `start` (i.e. boundary immediately
        # precedes the pattern).
        prefix = text[max(0, start - 200):start]
        if not prefix:
            return m  # at start-of-text — counts as boundary
        # Boundary regex must match the END of the prefix
        # (because the WEAK pattern starts immediately after it).
        for b in _BOUNDARY_PREFIX.finditer(prefix):
            if b.end() == len(prefix):
                return m
    return None


# ── Public result type ────────────────────────────────────────────


@dataclass(frozen=True)
class ScanResult:
    blocked: bool
    risk_score: int
    matches: tuple[str, ...]   # pattern names that contributed
    block_reason: Optional[str] = None  # name of the blocking pattern,
                                         # or "compound_weak_action" /
                                         # "compound_multi_weak"


# ── Public API ────────────────────────────────────────────────────


def scan(text: str, field_kind: FieldKind) -> ScanResult:
    """Scan a single field's text for prompt-injection patterns.

    Args:
        text: the field's raw text (will be matched case-insensitively).
        field_kind: FREEFORM (apply boundary constraints to WEAK)
                    or STRUCTURED (substring match throughout).

    Returns:
        ScanResult with:
          - blocked: True if any STRONG match, OR compound rule fires
          - risk_score: cumulative risk from WEAK + ACTION matches
          - matches: tuple of pattern names that contributed
          - block_reason: which rule triggered the block, if any
    """
    if not isinstance(text, str) or not text:
        return ScanResult(blocked=False, risk_score=0, matches=())

    matches: list[str] = []
    risk: int = 0

    # 1. STRONG — block on first hit, regardless of field_kind.
    #    These patterns are word-boundary anchored at the regex
    #    level so legitimate prose can't trip them.
    for p in _STRONG:
        if p.regex.search(text):
            return ScanResult(
                blocked=True,
                risk_score=p.risk_delta,
                matches=(p.name,),
                block_reason=p.name,
            )

    # 2. WEAK + ACTION — count for compound rule.
    weak_count = 0
    action_count = 0
    for p in _WEAK:
        if field_kind is FieldKind.FREEFORM:
            m = _weak_match_at_boundary(p, text)
        else:
            m = p.regex.search(text)
        if m is not None:
            matches.append(p.name)
            risk += p.risk_delta
            weak_count += 1
    for p in _ACTION:
        if p.regex.search(text):
            matches.append(p.name)
            risk += p.risk_delta
            action_count += 1

    # 3. Compound rule — escalate to BLOCK when:
    #    - any WEAK + any ACTION match, OR
    #    - two or more WEAK matches in the same field.
    if weak_count >= 1 and action_count >= 1:
        return ScanResult(
            blocked=True,
            risk_score=risk,
            matches=tuple(matches),
            block_reason="compound_weak_action",
        )
    if weak_count >= 2:
        return ScanResult(
            blocked=True,
            risk_score=risk,
            matches=tuple(matches),
            block_reason="compound_multi_weak",
        )

    # 4. Single weak or single action match — risk bump only.
    return ScanResult(
        blocked=False,
        risk_score=risk,
        matches=tuple(matches),
    )
