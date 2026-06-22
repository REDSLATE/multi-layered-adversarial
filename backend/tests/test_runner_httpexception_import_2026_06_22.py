"""Regression: `HTTPException` must be importable at module scope in
the in-process brain runner.

Why this exists (2026-06-22 P0 fix):

Production logs were flooded with
    `risedual.neutral_brains - WARNING - brain_vote post failed
     brain=gto sym=AVAH err=name 'HTTPException' is not defined`

Root cause: `from fastapi import HTTPException` was previously
declared INSIDE `_emit_intent`, but four sibling fire-and-forget
posters (`_post_directional_opinion`, `_post_brain_vote`, and two
other in-process loopback helpers) referenced `HTTPException` at
function scope. Every time one of those callers hit a 422/4xx, the
exception handler crashed with `NameError`, masking the real
rejection reason and burying the legitimate diagnostic in a
secondary stack-trace.

This test pins the hoist so a future "let's tidy imports" pass
can't silently re-localize the symbol.
"""
from __future__ import annotations

import sys

# `external/` lives at /app/external, sibling of /app/backend. The
# pytest invocation lives inside /app/backend so we widen the path
# to reach the runner module.
if "/app" not in sys.path:
    sys.path.insert(0, "/app")


def test_httpexception_imported_at_module_scope():
    """Top-level import of `HTTPException` MUST be present so every
    fire-and-forget poster shares the same name binding."""
    import importlib
    runner = importlib.import_module("external.brains.runner")

    # Direct attribute presence — set by `from fastapi import HTTPException`.
    assert hasattr(runner, "HTTPException"), (
        "external/brains/runner.py must import HTTPException at module "
        "scope. Sibling fire-and-forget posters depend on the name being "
        "globally available — local imports inside one method don't help "
        "the others, and the missing-name NameError gets caught as a "
        "second-order exception, masking the real 4xx rejection reason."
    )

    # Belt-and-braces: confirm it's the fastapi class, not something else.
    from fastapi import HTTPException as _expected
    assert runner.HTTPException is _expected, (
        "external/brains/runner.py:HTTPException must be the fastapi "
        "class — bridge tests + sibling handlers rely on identity match."
    )
