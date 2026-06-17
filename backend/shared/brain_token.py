"""Per-brain `<BRAIN>_INGEST_TOKEN` lookup with legacy fallback.

2026-02-20 — single chokepoint for the in-process runner's auth.

The canonical brain ID rename (alpha→camino, camaro→barracuda,
chevelle→hellcat, redeye→gto) flipped what env var each brain's
token lives under. The deploy env config on prod can lag behind the
code rename, so this helper tries the NEW env var first and falls
back to the LEGACY one. Both runner-side and MC-side auth call this
function so they always agree on which token is the right one.

Idiomatic use:
    expected = expected_ingest_token("gto")        # tries GTO_INGEST_TOKEN
                                                   # then REDEYE_INGEST_TOKEN
    if presented != expected:
        raise HTTPException(401, ...)
"""
from __future__ import annotations

import os


_LEGACY_BRAIN_FALLBACK_ENV: dict[str, str] = {
    "camino":    "ALPHA_INGEST_TOKEN",
    "barracuda": "CAMARO_INGEST_TOKEN",
    "hellcat":   "CHEVELLE_INGEST_TOKEN",
    "gto":       "REDEYE_INGEST_TOKEN",
}


def expected_ingest_token(brain: str) -> str:
    """Return the token a brain must present in `X-Runtime-Token` to
    authenticate to MC. Tries the canonical env var first, falls back
    to the legacy slot. Empty string if neither is set (which means
    auth WILL fail — by design, so misconfiguration is loud)."""
    if not brain:
        return ""
    primary = os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "") or ""
    if primary:
        return primary
    legacy_env = _LEGACY_BRAIN_FALLBACK_ENV.get(brain.lower())
    if legacy_env:
        return os.environ.get(legacy_env, "") or ""
    return ""
