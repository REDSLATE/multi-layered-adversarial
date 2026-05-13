"""Doctrine packet server — read-only Markdown doctrine for the brains.

Endpoint:
    GET /api/doctrine/{name}            → returns the doctrine markdown text
    GET /api/doctrine                   → lists available doctrine packets

Auth:
    Caller must pass X-Runtime-Token matching one of the four brains'
    ingest tokens. Doctrine is identical for every reader — no per-brain
    branching, no mutation, no write paths.

Storage:
    Doctrine packets live as plain Markdown at /app/runtime_patch_kit/
    and are filename-keyed (lowercase, .md auto-appended). The on-disk
    file IS the source of truth; this endpoint just exposes it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from runtime_auth import verify_runtime_token
from namespaces import DISCUSSION_PARTICIPANTS


DOCTRINE_DIR = Path("/app/runtime_patch_kit")
ALLOWED_SUFFIX = ".md"

# Registry of doctrine packets we publish. Mapping logical name → filename.
# Keep this explicit so we don't accidentally serve random files from the
# patch kit directory (e.g. tarballs, bundles, etc).
DOCTRINE_REGISTRY: dict[str, str] = {
    "vrl": "VRL_DOCTRINE.md",
    "discussion_layer": "DISCUSSION_LAYER_PATCH.md",
    "decision_machine": "decision_machine/DECISION_MACHINE_PATCH.md",
}


router = APIRouter(tags=["doctrine"])


def _resolve_caller(x_runtime_token: Optional[str]) -> str:
    """Identify which brain is calling, or 401.

    We try each known runtime's token until one matches — this avoids
    requiring the caller to declare itself in the URL.
    """
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    for rt in DISCUSSION_PARTICIPANTS:
        try:
            verify_runtime_token(rt, x_runtime_token)
            return rt
        except HTTPException:
            continue
    raise HTTPException(status_code=401, detail="invalid runtime ingest token")


@router.get("/doctrine")
async def list_doctrine(
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """List available doctrine packets."""
    caller = _resolve_caller(x_runtime_token)
    items = []
    for key, fname in DOCTRINE_REGISTRY.items():
        path = DOCTRINE_DIR / fname
        if not path.exists():
            continue
        items.append({
            "name": key,
            "filename": fname,
            "bytes": path.stat().st_size,
        })
    return {"caller": caller, "items": items, "count": len(items)}


@router.get("/doctrine/{name}")
async def get_doctrine(
    name: str,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Return the raw Markdown for a doctrine packet, read-only."""
    caller = _resolve_caller(x_runtime_token)
    key = name.lower().strip()
    if key not in DOCTRINE_REGISTRY:
        raise HTTPException(
            status_code=404,
            detail=f"unknown doctrine packet {name!r}; available: {sorted(DOCTRINE_REGISTRY)}",
        )
    path = DOCTRINE_DIR / DOCTRINE_REGISTRY[key]
    if not path.exists() or path.suffix != ALLOWED_SUFFIX:
        raise HTTPException(status_code=404, detail=f"doctrine file missing: {path.name}")
    text = path.read_text(encoding="utf-8")
    return {
        "caller": caller,
        "name": key,
        "filename": path.name,
        "bytes": len(text.encode("utf-8")),
        "content": text,
    }
