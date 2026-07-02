"""Per-brain `<BRAIN>_INGEST_TOKEN` lookup.

Single chokepoint for the in-process runner's auth. Each brain
presents `X-Runtime-Token` on ingest; MC compares it against the
env var `<BRAIN>_INGEST_TOKEN` (uppercased canonical brain id).

Idiomatic use:
    expected = expected_ingest_token("gto")   # reads GTO_INGEST_TOKEN
    if presented != expected:
        raise HTTPException(401, ...)

The legacy env-var slots (ALPHA/CAMARO/CHEVELLE/REDEYE_INGEST_TOKEN)
were retired 2026-07-03 — canonical names only.
"""
from __future__ import annotations

import os


def expected_ingest_token(brain: str) -> str:
    """Return the token a brain must present in `X-Runtime-Token` to
    authenticate to MC. Empty string if the env var isn't set — by
    design, so misconfiguration is loud."""
    if not brain:
        return ""
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "") or ""
