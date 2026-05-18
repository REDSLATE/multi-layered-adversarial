"""Runtime ingest token visibility — operator-facing read-back.

Doctrine:
    The per-brain `X-Runtime-Token` is just an env var on the MC server
    (`<BRAIN>_INGEST_TOKEN`). Operators historically had no way to see
    what value MC was expecting, so when a brain failed to authenticate
    there was no way to diagnose whether (a) the env var was missing on
    MC, (b) the brain's `MONOREPO_INGEST_TOKEN` had drifted, or (c) the
    token had been rotated on one side but not the other.

    This module exposes:
      * GET  /api/admin/runtime-tokens         — list status + masked preview
      * GET  /api/admin/runtime-tokens?reveal=true  — full plaintext, audited
      * GET  /api/admin/runtime-tokens/env-snippet  — downloadable .env
                                                       formatted to drop into
                                                       a brain host

    All routes require operator JWT. Every reveal writes one row to
    `roster_audit_log` so we have a paper trail of who looked at the
    plaintext token and when.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from auth import get_current_user
from db import db
from namespaces import DISCUSSION_PARTICIPANTS


router = APIRouter(prefix="/admin/runtime-tokens", tags=["runtime-tokens"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(token: str) -> str:
    """Mask a token so the operator can recognize it without revealing
    it. Keeps the first 14 chars (enough to see "<brain>-ingest-<4-hex>")
    and the last 4 chars, with bullets in the middle."""
    if not token:
        return ""
    if len(token) <= 22:
        return token[:6] + "●●●●" + token[-4:] if len(token) > 10 else "●●●●"
    return f"{token[:14]}●●●●●●●●{token[-4:]}"


def _env_var_for(runtime: str) -> str:
    return f"{runtime.upper()}_INGEST_TOKEN"


async def _audit_reveal(*, brain: str, actor_email: str) -> None:
    await db["roster_audit_log"].insert_one({
        "action": "reveal_runtime_token",
        "brain": brain,
        "actor": actor_email,
        "ts": _now_iso(),
    })


@router.get("")
async def list_runtime_tokens(
    reveal: bool = Query(default=False, description="When True, return the plaintext token (audited)."),
    brain: str | None = Query(default=None, description="If set, only return this brain's row (also revealed if reveal=true)."),
    user: dict = Depends(get_current_user),
):
    """Operator-facing read-back of the per-brain ingest tokens.

    Without `reveal`: returns masked previews + a configured flag.
    With `reveal=true`: returns the full plaintext token and writes
    one audit row per revealed brain to `roster_audit_log`.
    """
    runtimes = [brain] if brain else list(DISCUSSION_PARTICIPANTS)
    items: list[dict] = []
    for rt in runtimes:
        if rt not in DISCUSSION_PARTICIPANTS:
            continue
        env_name = _env_var_for(rt)
        raw = os.environ.get(env_name) or ""
        configured = bool(raw)
        row: dict = {
            "runtime": rt,
            "env_var": env_name,
            "configured": configured,
            "token_preview": _mask(raw) if configured else None,
            "length": len(raw) if configured else 0,
        }
        if reveal and configured:
            row["token"] = raw
            await _audit_reveal(
                brain=rt,
                actor_email=(user or {}).get("email") or "unknown",
            )
        items.append(row)
    return {
        "items": items,
        "count": len(items),
        "doctrine": (
            "Each brain's MONOREPO_INGEST_TOKEN must equal MC's "
            "<BRAIN>_INGEST_TOKEN env var. Mismatches → 401 on ingest. "
            "Rotate by updating BOTH sides simultaneously; brains using "
            "the old value will lock out until they re-key."
        ),
    }


@router.get("/env-snippet", response_class=PlainTextResponse)
async def env_snippet(
    brain: str = Query(..., description="Which brain to generate the snippet for."),
    user: dict = Depends(get_current_user),
):
    """Return a `.env` snippet the operator can drop into a brain host.

    Format:
        # RISEDUAL Mission Control — <brain> ingest credentials
        # Generated: 2026-05-17T...Z by <operator>
        MONOREPO_BASE_URL=https://mission.risedual.ai
        MONOREPO_INGEST_TOKEN=<plaintext>
        RUNTIME_NAME=<brain>

    Each generation writes one audit row (treats it as a reveal).
    """
    if brain not in DISCUSSION_PARTICIPANTS:
        return PlainTextResponse(
            f"# ERROR: unknown brain {brain!r}. Valid: {sorted(DISCUSSION_PARTICIPANTS)}\n",
            status_code=400,
        )
    env_name = _env_var_for(brain)
    raw = os.environ.get(env_name) or ""
    if not raw:
        return PlainTextResponse(
            (
                f"# ERROR: {env_name} is not configured on Mission Control.\n"
                f"# Set this env var on the MC deployment, redeploy, then "
                f"regenerate this snippet.\n"
            ),
            status_code=404,
        )
    await _audit_reveal(
        brain=brain,
        actor_email=(user or {}).get("email") or "unknown",
    )
    snippet = (
        f"# RISEDUAL Mission Control — {brain} ingest credentials\n"
        f"# Generated: {_now_iso()} by {(user or {}).get('email') or 'unknown'}\n"
        f"# Drop into the brain host's .env file (the same dir as your\n"
        f"# brain's runtime sidecar). Keep this file out of version control.\n"
        f"\n"
        f"MONOREPO_BASE_URL=https://mission.risedual.ai\n"
        f"MONOREPO_INGEST_TOKEN={raw}\n"
        f"RUNTIME_NAME={brain}\n"
    )
    return PlainTextResponse(
        snippet,
        headers={
            "Content-Disposition": f'attachment; filename="{brain}.env"',
        },
    )
