"""Position primitive — Pydantic request/response models.

Extracted from `shared/positions.py` in 2026-07-12 P6a to reduce
the state-machine module below its 1000-line ceiling. NO behavior
change — same validators, same field defaults, same semantics.

Doctrine note: `StanceIn.source_bar_close_at` (2026-07-12 5.b)
enables the v2 consensus fingerprint. When ALL engaged brains
supply it, the state machine gates on freshness spread before
advancing to `consensus_long`/`consensus_short`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from namespaces import DISCUSSION_PARTICIPANTS


BrainT = Literal["camino", "barracuda", "hellcat", "gto"]
StanceT = Literal["long", "short", "abstain"]
DirectionT = Literal["long", "short"]


CALL_MODE_AUTO = "auto"
CALL_MODE_MANUAL = "manual"
VALID_CALL_MODES = frozenset({CALL_MODE_AUTO, CALL_MODE_MANUAL})


class ProposeIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    regime_tag: Optional[str] = Field(default=None, max_length=48)
    thesis: str = Field("", max_length=2048)
    proposed_by: str = Field(..., description="brain name or 'operator'")
    call_mode: Literal["auto", "manual"] = Field(
        default="manual",
        description=(
            "auto: the executor seat's long/short stance immediately "
            "advances state. manual: operator clicks CALL LONG / CALL "
            "SHORT to advance."
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("proposed_by")
    @classmethod
    def _proposed_by_check(cls, v: str) -> str:
        v = v.strip().lower()
        if v != "operator" and v not in DISCUSSION_PARTICIPANTS:
            raise ValueError(
                f"proposed_by must be 'operator' or one of {DISCUSSION_PARTICIPANTS}"
            )
        return v


class StanceIn(BaseModel):
    stance: StanceT
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    notes: str = Field("", max_length=2048)
    # Memory provenance (optional — brains opt in once they emit it).
    # When a brain reports which memory artefacts shaped this stance,
    # we record them so future audits can trace memory poisoning, stale
    # priors, or reinforcement loops. Empty list is acceptable.
    memory_sources: list[str] = Field(default_factory=list, max_length=32)
    # Confidence origin breakdown (optional). Brains that can decompose
    # their confidence into named components (model, memory,
    # contradiction_penalty, regime_alignment, …) report them here.
    # Validated below to keep keys/values bounded.
    confidence_origin: dict[str, float] = Field(default_factory=dict)
    # ── 2026-07-12 doctrine step 5.b: fresh-input provenance ──
    source_bar_close_at: Optional[str] = Field(
        default=None, max_length=64,
        description=(
            "ISO-8601 close ts of the bar this stance was derived from. "
            "Enables the v2 consensus fingerprint + fresh-input gate."
        ),
    )

    @field_validator("memory_sources")
    @classmethod
    def _norm_sources(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            if not isinstance(s, str):
                raise ValueError("memory_sources must be strings")
            t = s.strip()
            if not t:
                continue
            if len(t) > 128:
                raise ValueError(f"memory_source too long: {t[:32]}...")
            out.append(t)
        return out

    @field_validator("confidence_origin")
    @classmethod
    def _norm_confidence_origin(cls, v: dict) -> dict[str, float]:
        if len(v) > 12:
            raise ValueError("confidence_origin can have at most 12 components")
        out: dict[str, float] = {}
        for k, val in v.items():
            if not isinstance(k, str) or not k:
                raise ValueError("confidence_origin keys must be non-empty strings")
            if len(k) > 64:
                raise ValueError(f"confidence_origin key too long: {k[:32]}...")
            try:
                f = float(val)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"confidence_origin[{k!r}] must be a number"
                ) from e
            if not (-1.0 <= f <= 1.0):
                raise ValueError(
                    f"confidence_origin[{k!r}]={f} must be in [-1, 1]"
                )
            out[k.strip()] = f
        return out


class OperatorStanceIn(StanceIn):
    """Operator posting a stance on behalf of a brain (or themselves)."""
    brain: BrainT


class ExecutorCallIn(BaseModel):
    """Operator advances the position via the executor seat's decision.
    direction='long' → consensus_long; 'short' → consensus_short;
    a separate /reject endpoint handles walk-away.
    """
    direction: DirectionT
    notes: str = Field("", max_length=2048)


class RejectIn(BaseModel):
    notes: str = Field("", max_length=2048)
