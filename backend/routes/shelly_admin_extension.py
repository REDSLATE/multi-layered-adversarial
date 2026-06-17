"""MC-Shelly extension admin endpoints (L3 + L6 + MEMORY.md).

Mounted under `/api/admin/shelly/` so the operator can fire the
verified-fact certifier, the wiki curator, and pull a brain's
MEMORY.md profile without leaving the UI.

Authority pin: every endpoint here is ADVISORY_ONLY surface for
write+read of two new collections (`shelly_verified_facts`,
`risedual_wiki`) and pure-read of LocalShelly memory. Nothing here
executes, blocks, or promotes a brain.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from auth import get_current_user
from shelly.memory_profile import render_brain_memory_md
from shelly.verified_facts import (
    auto_certify_scan,
    certify_one,
    curate_wiki_run,
    verified_facts_summary,
    wiki_lookup,
    wiki_summary,
)


router = APIRouter(prefix="/admin/shelly", tags=["shelly-extension"])


# ──────────────────────── L3: verified facts ────────────────────────


class CertifyBody(BaseModel):
    event_hash: str
    note: str | None = None


@router.post("/verified-facts/certify")
def certify_event(
    body: CertifyBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Operator certifies a single shared-memory event as verified
    fact. Idempotent — re-certifying returns `already_verified`."""
    result = certify_one(
        body.event_hash,
        via="operator",
        operator=user.get("email"),
        note=body.note,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason"))
    return result


@router.post("/verified-facts/auto-scan")
def auto_certify(
    limit: int = Query(200, ge=1, le=2000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Scan shared memory for event_hashes that have converged across
    ≥ 3 brains AND have ≥ 1 resolved outcome. Auto-certify them."""
    return auto_certify_scan(limit=limit)


@router.get("/verified-facts/summary")
def facts_summary(_user: dict = Depends(get_current_user)):  # noqa: B008
    return verified_facts_summary()


# ──────────────────────── L6: RISEDUAL wiki ────────────────────────


@router.post("/wiki/curate")
def wiki_curate(
    limit: int = Query(100, ge=1, le=1000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Re-build wiki entries from current verified facts. Idempotent.
    Bounded by `limit` topics per run."""
    return curate_wiki_run(limit=limit)


@router.get("/wiki/summary")
def wiki_summary_route(_user: dict = Depends(get_current_user)):  # noqa: B008
    return wiki_summary()


@router.get("/wiki/lookup")
def wiki_lookup_route(
    symbol: str = Query(..., min_length=1),
    direction: Literal["BUY", "SELL", "SHORT", "HOLD", "COVER"] | None = None,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """What has RISEDUAL learned about SYMBOL [+ direction]?"""
    return {"ok": True, "entries": wiki_lookup(symbol, direction)}


# ──────────────────────── MEMORY.md ────────────────────────


@router.get("/memory-md/{brain}", response_class=PlainTextResponse)
def brain_memory_md(
    brain: str,
    recent_limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> str:
    """Render one brain's LocalShelly state as MEMORY.md. Read-only.
    Returns plain text/markdown — operator pastes into editor or the
    frontend renders as <pre>."""
    brain_l = brain.lower()
    if brain_l not in ("camino", "barracuda", "hellcat", "gto"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown brain {brain!r}; expected alpha/camaro/chevelle/redeye",
        )
    return render_brain_memory_md(brain_l, recent_limit=recent_limit)


@router.get("/memory-md", response_class=PlainTextResponse)
def all_brain_memory_md(
    recent_limit: int = Query(20, ge=1, le=200),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> str:
    """Render all four brains' MEMORY.md concatenated, with separators.
    Lower default recent_limit since 4× the volume."""
    parts: list[str] = []
    for brain in ("camino", "barracuda", "hellcat", "gto"):
        parts.append(render_brain_memory_md(brain, recent_limit=recent_limit))
        parts.append("\n---\n")
    return "\n".join(parts)
