"""Operator-side bundle download endpoint.

Lets the operator browser-download portable patch-kit archives out of
the preview / PROD pod so they can drop them into the brain-stack
repos that live elsewhere. JWT-gated (admin only) and registry-locked
so we can never accidentally serve arbitrary disk files.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from auth import get_current_user


router = APIRouter(prefix="/admin/runtime-bundles", tags=["runtime-bundles"])


_BUNDLE_DIR = Path("/app/runtime_patch_kit/bundles")

# Whitelist of bundles operator may download. Any new bundle must be
# explicitly registered here — no directory listings, no path traversal.
BUNDLE_REGISTRY: dict[str, dict] = {
    "platform_survival.tar.gz": {
        "patch": "platform_survival",
        "media_type": "application/gzip",
        "doctrine_note": (
            "Sidecars communicate · MC approves · RoadGuard protects · "
            "broker executes only with MC receipt · preview is not PROD."
        ),
    },
    "platform_survival.zip": {
        "patch": "platform_survival",
        "media_type": "application/zip",
        "doctrine_note": (
            "Sidecars communicate · MC approves · RoadGuard protects · "
            "broker executes only with MC receipt · preview is not PROD."
        ),
    },
}


def _bundle_sha(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


@router.get("")
async def list_bundles(_user: dict = Depends(get_current_user)):
    """Manifest of downloadable bundles + sha256 + bytes so the operator
    can verify integrity after transfer.
    """
    items = []
    for filename, spec in BUNDLE_REGISTRY.items():
        path = _BUNDLE_DIR / filename
        if not path.exists():
            items.append({
                "filename": filename,
                "patch": spec["patch"],
                "present": False,
                "doctrine_note": spec["doctrine_note"],
            })
            continue
        items.append({
            "filename": filename,
            "patch": spec["patch"],
            "present": True,
            "bytes": path.stat().st_size,
            "sha256": _bundle_sha(path),
            "media_type": spec["media_type"],
            "doctrine_note": spec["doctrine_note"],
            "download_url": f"/api/admin/runtime-bundles/{filename}",
        })
    return {"bundles": items, "count": len(items)}


@router.get("/{filename}")
async def download_bundle(filename: str, _user: dict = Depends(get_current_user)):
    if filename not in BUNDLE_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown bundle {filename!r}")
    path = _BUNDLE_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"bundle missing on disk: {filename}")
    media_type = BUNDLE_REGISTRY[filename]["media_type"]
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=filename,
        headers={
            "X-Bundle-Sha256": _bundle_sha(path) or "",
            "Cache-Control": "no-store",
        },
    )
