"""NegativeKnowledge — pattern-based abstention.

The brain consults this before voting. If the current setup looks
like a previously-recorded failure pattern in the current regime,
the brain returns `BrainVote.abstain(...)` instead of a BUY/SELL.

Verifier learning path (out-of-hot-path): after a losing trade the
verifier calls `learn_from_failure(setup_embedding, regime, loss_bps)`.
The pattern is either reinforced (similar pattern exists) or
appended. The brain's next pre-vote check will catch the same
setup.

Doctrine: the brain owns the abstain decision (it's a brain opinion).
The verifier owns the learning step (it's a verifier conclusion).
They never cross the IP boundary — the verifier queues updates; the
brain ingests them at maintenance time, not in the hot path.

Persistence: in-memory by design (Paradox v2 build-order step 4 —
no vector store yet). The `_similarity` method is a hashable
placeholder until the embedding pipeline lands; the verifier-driven
learning flow works the same regardless of similarity backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NegativePattern:
    pattern_hash: str
    regime: str
    false_positive_count: int
    regret_score: float  # cumulative |loss_bps|; higher = worse pattern
    last_triggered: datetime


class NegativeKnowledge:
    def __init__(
        self,
        brain_id: str,
        similarity_threshold: float = 0.85,
    ) -> None:
        self.brain_id = brain_id
        self.threshold = similarity_threshold
        self._patterns: list[NegativePattern] = []

    # ── pre-vote check (brain hot path) ───────────────────────────────

    def check(
        self,
        setup_embedding: str,
        regime: str,
    ) -> tuple[bool, Optional[str]]:
        """Returns (should_abstain, reason). The brain calls this
        BEFORE deciding stance. If True, it must call
        `BrainVote.abstain(...)` with the returned reason."""
        for pattern in self._patterns:
            if pattern.regime != regime:
                continue
            sim = self._similarity(setup_embedding, pattern.pattern_hash)
            if sim > self.threshold:
                return True, (
                    f"negative_pattern:{pattern.pattern_hash}:"
                    f"regime={regime}:fp_count={pattern.false_positive_count}:"
                    f"regret={pattern.regret_score:.1f}bps"
                )
        return False, None

    # ── verifier-driven learning (out of hot path) ────────────────────

    def learn_from_failure(
        self,
        setup_embedding: str,
        regime: str,
        loss_bps: float,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Verifier calls this after a losing trade is attributed.
        Either reinforces an existing pattern (similar match >0.9) or
        records a brand-new one."""
        now = timestamp or _now_utc()
        for pattern in self._patterns:
            if pattern.regime != regime:
                continue
            if self._similarity(setup_embedding, pattern.pattern_hash) > 0.9:
                pattern.false_positive_count += 1
                pattern.regret_score += abs(loss_bps)
                pattern.last_triggered = now
                return
        self._patterns.append(NegativePattern(
            pattern_hash=setup_embedding,
            regime=regime,
            false_positive_count=1,
            regret_score=abs(loss_bps),
            last_triggered=now,
        ))

    # ── inspection helpers ────────────────────────────────────────────

    def pattern_count(self) -> int:
        return len(self._patterns)

    def patterns_for_regime(self, regime: str) -> list[NegativePattern]:
        return [p for p in self._patterns if p.regime == regime]

    # ── similarity backend (swap when vector store lands) ─────────────

    def _similarity(self, a: str, b: str) -> float:
        """Placeholder similarity. Until the embedding pipeline ships
        we use a hash-prefix comparator that lets unit tests pin the
        learning flow without depending on a vector backend.

        When the real embedding store lands, replace this with cosine
        similarity over the embedding vectors. The learning interface
        stays identical."""
        if a == b:
            return 1.0
        if len(a) >= 8 and len(b) >= 8 and a[:8] == b[:8]:
            return 0.9
        if len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4]:
            return 0.5
        return 0.0
