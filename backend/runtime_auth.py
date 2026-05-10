"""Per-runtime ingest token validation.
Each runtime sends X-Runtime-Token. The token must match the env var for the
runtime claimed in the request body/path. This prevents Alpha from impersonating
Camaro or Chevelle, even if its token leaks.

Advisors (REDEYE) also authenticate here for the discussion-layer endpoints.
"""
import os
from fastapi import Header, HTTPException

from namespaces import DISCUSSION_PARTICIPANTS


def _expected_token(runtime: str) -> str | None:
    return os.environ.get(f"{runtime.upper()}_INGEST_TOKEN")


def verify_runtime_token(runtime: str, x_runtime_token: str) -> None:
    if runtime not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(
            status_code=400,
            detail=f"runtime must be one of {DISCUSSION_PARTICIPANTS}",
        )
    expected = _expected_token(runtime)
    if not expected:
        raise HTTPException(status_code=503, detail=f"ingest token for {runtime} is not configured")
    if not x_runtime_token or x_runtime_token != expected:
        raise HTTPException(status_code=401, detail="invalid runtime ingest token")


async def runtime_token_dep(
    runtime: str,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
) -> str:
    """For path-style endpoints like /runtime/{runtime}/heartbeat."""
    verify_runtime_token(runtime, x_runtime_token or "")
    return runtime
