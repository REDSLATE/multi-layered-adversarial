"""Verifier Rule Sheet — labeled-lesson layer.

One labeled record per intent: brain authored it, what it claimed,
what the research said, what the seat decided, what the governor
sized, what the broker filled, and what eventually happened to the
position. The dataset is the training corpus for the brains.

Doctrine (2026-02-20 operator pin):
    "Every intent becomes a labeled lesson."
    "Train from the verifier, not vibes."

This package READS only. It does not mutate intents, does not write
to the gate chain, does not write back to the brain runtime. Lessons
land in `shared_lessons` (write-once after the position is resolved)
and are queryable via `shared/lessons/builder.build_lesson(intent_id)`
or in bulk via the routes module.
"""
from .schemas import Lesson, LessonOutcome  # noqa: F401
from .builder import build_lesson, build_lessons_bulk  # noqa: F401
from .setup_classifier import classify_setup  # noqa: F401
