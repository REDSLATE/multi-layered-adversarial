"""RISE AI shared package — role profiles + prompt composer.

Single source of truth imported by every brain pod (Alpha / Camaro /
Chevelle / RedEye) so role focus, forbidden lists, model ids, and
prompt scaffolding never drift across the fleet.

Public surface:
    RISE_AI_ROLE_PROFILES      — raw registry, brain → profile.
    GENERAL_PROFILE            — fallback for unknown brains.
    profile_for(role)          — dict lookup with graceful fallback.
    model_for_role(role)       — checkpoint model_id lookup.
    compose_role_aligned_prompt(...) — canonical brain-prompt builder.

Authority: data + pure functions. Zero I/O. Zero execution surface.
"""
from .prompt_composer import compose_role_aligned_prompt
from .role_profiles import (
    GENERAL_PROFILE,
    RISE_AI_ROLE_PROFILES,
    model_for_role,
    profile_for,
)
from .auto_grader import (
    AUTO_GRADER_ROLE,
    TRAINABLE_ROLES,
    compose_grading_prompt,
    grade_batch,
    grade_one,
    parse_grade,
)

__all__ = [
    "AUTO_GRADER_ROLE",
    "GENERAL_PROFILE",
    "RISE_AI_ROLE_PROFILES",
    "TRAINABLE_ROLES",
    "compose_grading_prompt",
    "compose_role_aligned_prompt",
    "grade_batch",
    "grade_one",
    "model_for_role",
    "parse_grade",
    "profile_for",
]
