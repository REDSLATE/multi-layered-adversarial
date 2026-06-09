"""Skill-file loader.

Each skill lives at `external/skills/skill_pack/<name>/SKILL.md` and
has YAML-style frontmatter:

    ---
    name: crypto-execution
    description: Evaluates crypto trade readiness ...
    tags: crypto, btc, eth, kraken, execution, spread, liquidity
    ---

    # Crypto Execution

    ## Rules
    1. Do not authorize execution directly.
    ...

The loader is intentionally simple — no jinja, no DSL. Operators can
edit any SKILL.md, drop new ones into `skill_pack/`, and the next
selector call picks them up (load is fresh per call so hot-edit works
without restart).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


logger = logging.getLogger("risedual.skills")


# Default location relative to the repo root. Resolved against /app
# at runtime since `external.skills` is loaded via the same sys.path
# trick the brain runner uses.
_DEFAULT_SKILL_ROOT = "/app/external/skills/skill_pack"


@dataclass
class Skill:
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    body: str = ""
    path: Optional[Path] = None


class SkillLoadError(ValueError):
    """Raised when a SKILL.md file is missing required frontmatter."""


class SkillLoader:
    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root or _DEFAULT_SKILL_ROOT)

    def load_all(self) -> list[Skill]:
        """Read every SKILL.md under `root`. Bad files are SKIPPED
        with a warning — one malformed skill must NOT take down the
        whole runtime."""
        if not self.root.exists():
            logger.warning("skill_pack not found at %s", self.root)
            return []
        out: list[Skill] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            try:
                raw = path.read_text(encoding="utf-8")
                skill = self._parse(raw, path)
                out.append(skill)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "skill load failed path=%s err=%s — skipping", path, exc,
                )
        return out

    def _parse(self, raw: str, path: Path) -> Skill:
        match = re.search(r"---(.*?)---", raw, re.DOTALL)
        if not match:
            raise SkillLoadError(f"{path}: missing --- frontmatter ---")
        meta = match.group(1)
        body = raw[match.end():].strip()

        name = self._field(meta, "name", path=path)
        description = self._field(meta, "description", path=path)
        tags_raw = self._field(meta, "tags", default="", path=path)
        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

        return Skill(
            name=name,
            description=description,
            tags=tags,
            body=body,
            path=path,
        )

    @staticmethod
    def _field(
        meta: str,
        key: str,
        default: Optional[str] = None,
        path: Optional[Path] = None,
    ) -> str:
        match = re.search(rf"{key}:\s*(.+)", meta)
        if not match:
            if default is not None:
                return default
            raise SkillLoadError(
                f"{path or '<unknown>'}: missing required field `{key}`"
            )
        return match.group(1).strip()


__all__ = ["Skill", "SkillLoader", "SkillLoadError"]
