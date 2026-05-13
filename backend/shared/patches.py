"""Patch distribution — MC serves drop-in code modules to the four brains.

This is the next-level companion to /api/doctrine. Doctrine is read-only
prose. Patches are read-only code. A brain authenticates with its
runtime token, pulls a manifest of files, then pulls each file. The
sidecar writes them to disk and runs the install hint.

Endpoints:
    GET /api/patches                          → list available patches
    GET /api/patches/{name}/manifest          → file list with sha256 + bytes
    GET /api/patches/{name}/file/{path:path}  → raw file content

Auth: any of the four brains' X-Runtime-Token. Every fetch is logged to
`shared_patch_pulls` for audit.

Doctrine:
  * Patches are authored on the MC pod by the operator (no upload
    endpoint exists — operator commits to the repo, then deploys MC).
  * Patches are immutable from the brain's perspective.
  * Every pull is audit-logged with caller, file, ts.
  * Paths are registry-gated — no arbitrary file disclosure.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from db import db
from namespaces import RUNTIMES
from runtime_auth import verify_runtime_token


router = APIRouter(tags=["patches"])

PATCH_BASE = Path("/app/runtime_patch_kit")
PATCH_PULLS_COLLECTION = "shared_patch_pulls"

# Registry of distributable patches. Each entry maps a logical patch name
# to (root_dir, list_of_relative_files). Only files listed here are
# served — bundle archives and doctrine markdown live elsewhere.
PATCH_REGISTRY: dict[str, dict] = {
    "decision_machine": {
        "root": PATCH_BASE / "decision_machine",
        "files": [
            "decision_machine.py",
            "DECISION_MACHINE_PATCH.md",
        ],
        "install_hint": (
            "Copy decision_machine.py to services/decision_machine.py in your "
            "sidecar. Wire it into your tick loop per the .md guide. Set "
            "DECISION_MACHINE_ENABLED=true to activate."
        ),
        "version": "1.0",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_caller(x_runtime_token: Optional[str]) -> str:
    if not x_runtime_token:
        raise HTTPException(status_code=401, detail="X-Runtime-Token required")
    for rt in RUNTIMES:
        try:
            verify_runtime_token(rt, x_runtime_token)
            return rt
        except HTTPException:
            continue
    raise HTTPException(status_code=401, detail="invalid runtime ingest token")


def _safe_path(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, refusing any escape."""
    candidate = (root / rel).resolve()
    root_abs = root.resolve()
    if not str(candidate).startswith(str(root_abs) + "/") and candidate != root_abs:
        raise HTTPException(status_code=400, detail="path traversal rejected")
    return candidate


async def _log_pull(*, caller: str, patch: str, file: str | None = None, status: str = "ok") -> None:
    try:
        await db[PATCH_PULLS_COLLECTION].insert_one({
            "caller": caller,
            "patch": patch,
            "file": file,
            "status": status,
            "ts": _now_iso(),
        })
    except Exception:  # noqa: BLE001  audit must never break the request path
        pass


# ─────────────────────────────── routes ───────────────────────────────

@router.get("/patches/install.sh", response_class=None)
async def installer_script(
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    """Serve the bash installer as raw text/x-shellscript.

    Brain one-liner:
        curl -s "$MC/api/patches/install.sh" \\
          -H "X-Runtime-Token: $TOKEN" \\
          | bash -s -- decision_machine services
    """
    from fastapi.responses import PlainTextResponse
    caller = _resolve_caller(x_runtime_token)
    installer = PATCH_BASE / "install_patch.sh"
    if not installer.exists():
        raise HTTPException(status_code=404, detail="installer missing on MC disk")
    text = installer.read_text(encoding="utf-8")
    await _log_pull(caller=caller, patch="_installer", file="install_patch.sh", status="installer")
    return PlainTextResponse(text, media_type="text/x-shellscript")


@router.get("/patches")
async def list_patches(
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    caller = _resolve_caller(x_runtime_token)
    items = []
    for name, spec in PATCH_REGISTRY.items():
        root: Path = spec["root"]
        if not root.exists():
            continue
        items.append({
            "name": name,
            "version": spec.get("version", "1.0"),
            "files": list(spec["files"]),
            "install_hint": spec.get("install_hint", ""),
        })
    await _log_pull(caller=caller, patch="*", file=None, status="list")
    return {"caller": caller, "items": items, "count": len(items)}


@router.get("/patches/{name}/manifest")
async def patch_manifest(
    name: str,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    caller = _resolve_caller(x_runtime_token)
    key = name.lower().strip()
    if key not in PATCH_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown patch {name!r}")
    spec = PATCH_REGISTRY[key]
    root: Path = spec["root"]
    files = []
    for rel in spec["files"]:
        path = _safe_path(root, rel)
        if not path.exists() or not path.is_file():
            files.append({"path": rel, "bytes": 0, "sha256": None, "present": False})
            continue
        content = path.read_bytes()
        files.append({
            "path": rel,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "present": True,
        })
    await _log_pull(caller=caller, patch=key, file=None, status="manifest")
    return {
        "caller": caller,
        "name": key,
        "version": spec.get("version", "1.0"),
        "install_hint": spec.get("install_hint", ""),
        "files": files,
        "count": len(files),
    }


@router.get("/patches/{name}/file/{filepath:path}")
async def patch_file(
    name: str,
    filepath: str,
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
):
    caller = _resolve_caller(x_runtime_token)
    key = name.lower().strip()
    if key not in PATCH_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown patch {name!r}")
    spec = PATCH_REGISTRY[key]
    if filepath not in spec["files"]:
        # File must be explicitly registered. No directory listings.
        raise HTTPException(status_code=404, detail=f"file {filepath!r} not in patch manifest")
    root: Path = spec["root"]
    path = _safe_path(root, filepath)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"file missing on MC disk: {filepath}")
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    await _log_pull(caller=caller, patch=key, file=filepath, status="file")
    return {
        "caller": caller,
        "name": key,
        "path": filepath,
        "bytes": len(text.encode("utf-8")),
        "sha256": sha,
        "content": text,
    }
