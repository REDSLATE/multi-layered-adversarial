"""Skill selector.

Scores every loaded skill against a (task, snapshot) context and
returns the top-N. Scoring is intentionally simple:

  * Tag hits are weighted 3x (precise authorial intent)
  * Description-word hits are weighted 1x (broad lexical match)

If the operator wants smarter selection later (embedding similarity,
LLM-based picker), this is the surface to swap.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from external.skills.loader import Skill, SkillLoader


logger = logging.getLogger("risedual.skills.selector")


_WORD_RE = re.compile(r"[a-z0-9/]+")


def _tokens(text: str) -> set[str]:
    """Lowercased, regex-extracted tokens. Strips punctuation so
    `btc.` and `btc,` both match the tag `btc`."""
    return set(_WORD_RE.findall((text or "").lower()))


class SkillSelector:
    def __init__(self, loader: Optional[SkillLoader] = None) -> None:
        self.loader = loader or SkillLoader()

    def select(
        self,
        task: str,
        snapshot: Optional[dict[str, Any]] = None,
        limit: int = 3,
    ) -> list[Skill]:
        skills = self.loader.load_all()
        if not skills:
            return []

        # Compose the context text from task + snapshot. Snapshot
        # is flattened to "key=value" tokens so things like
        # `symbol=BTC/USD` contribute the `btc` and `usd` tokens.
        ctx = task or ""
        if snapshot:
            for k, v in snapshot.items():
                ctx += f" {k}={v}"
        ctx_tokens = _tokens(ctx)

        scored: list[tuple[int, Skill]] = []
        for skill in skills:
            score = 0
            # Tag hits — heaviest weight (operator-curated triggers).
            for tag in skill.tags:
                # Multi-word tags ("market memory") are split on
                # whitespace so partial matches still credit.
                for token in _tokens(tag):
                    if token in ctx_tokens:
                        score += 3
            # Description-word hits — broader lexical fallback.
            for token in _tokens(skill.description):
                if token in ctx_tokens:
                    score += 1
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda s: (-s[0], s[1].name))
        return [skill for _, skill in scored[: max(1, limit)]]


__all__ = ["SkillSelector"]
