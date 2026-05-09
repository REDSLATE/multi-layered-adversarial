"""Sidecar client for writing observation data to the RISEDUAL monorepo.
Per doctrine: this is the ONLY file in this runtime that knows about the
monorepo's shared collections. Decision logic stays out of here.

Drop this file into your runtime's backend (e.g. backend/services/risedual_monorepo_client.py).
Then add three vars to your runtime's .env:

    MONOREPO_BASE_URL=https://<your-monorepo-host>
    MONOREPO_INGEST_TOKEN=<token-for-this-runtime>
    RUNTIME_NAME=alpha    # or camaro / chevelle

Then call the helpers below alongside your existing local audit / firewall / calibration writes.
The monorepo is a MIRROR, never a replacement. Local writes stay untouched.

Failures NEVER raise — the monorepo being down must not take down a runtime.
"""
from __future__ import annotations

import os
import logging
import httpx

log = logging.getLogger("risedual.monorepo_client")


def _base() -> str:
    return os.environ["MONOREPO_BASE_URL"].rstrip("/")


def _token() -> str:
    return os.environ["MONOREPO_INGEST_TOKEN"]


def _runtime() -> str:
    return os.environ["RUNTIME_NAME"]


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


async def _post(path: str, body: dict) -> dict:
    body = {"runtime": _runtime(), **body}
    try:
        r = await _get_client().post(
            f"{_base()}/api/ingest/{path}",
            json=body,
            headers={"X-Runtime-Token": _token()},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("monorepo ingest %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


# -------- Public helpers (call these alongside your existing local writes) --------

async def emit_receipt(action: str, intent: dict, executed: bool = False) -> dict:
    """Mirror an ADL receipt to the monorepo. Observation invariant is enforced
    server-side: executed=True is coerced to False unless BROKER_LIVE_ORDER_ENABLED
    is true on the monorepo."""
    return await _post("receipts", {"action": action, "intent": intent or {}, "executed": bool(executed)})


async def emit_memory_label(label: str, reason: str = "", payload_summary: str = "") -> dict:
    """Mirror a memory firewall label. label must be 'safe' | 'review' | 'quarantine'."""
    return await _post("memory-labels", {
        "label": label, "reason": reason or "", "payload_summary": payload_summary or "",
    })


async def register_calibrator(name: str, version: str, method: str, fit_at: str | None = None) -> dict:
    """Idempotent register/update of a calibrator's metadata. Call after every refit."""
    return await _post("calibrators", {
        "name": name, "version": version, "method": method, "fit_at": fit_at,
    })


async def register_artifact(artifact: str, version: str, sha: str, registered_at: str | None = None) -> dict:
    """Idempotent register/update of a model artifact. Call at startup + on retrain."""
    return await _post("artifacts", {
        "artifact": artifact, "version": version, "sha": sha, "registered_at": registered_at,
    })


async def heartbeat(status: str = "ok", detail: dict | None = None) -> dict:
    """Liveness ping. Call every 30-60s in a background task."""
    return await _post("heartbeat", {"status": status, "detail": detail or {}})


async def emit_promotion_artifact(
    target_authority: str,
    metrics: dict,
    notes: str = "",
) -> dict:
    """Patent G — runtime files evidence that it has met the bar for an
    authority elevation. Server stores it; the operator decides via
    Patent J + countersign.

    Promotion never happens automatically — this only files evidence.

    target_authority: 'challenger' | 'advisor' | 'co_trader' | 'primary'
    metrics: should include keys 'ece' (float), 'brier' (float),
             'resolved_rows' (int), 'disagreement_stability' (float),
             'audit_integrity_pass' (bool). Extra keys are stored verbatim.
    """
    return await _post("promotion-artifact", {
        "target_authority": target_authority,
        "metrics": metrics or {},
        "notes": notes or "",
    })
