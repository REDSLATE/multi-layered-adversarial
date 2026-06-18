"""Kraken Pro connection routes + in-app auto-poller.

Endpoints (all operator-JWT authenticated):
    POST   /api/admin/kraken/connect       Save encrypted credentials,
                                            run scope probe, start poller.
    GET    /api/admin/kraken/status        Connection summary (redacted).
    POST   /api/admin/kraken/reprobe       Re-run the scope probe.
    POST   /api/admin/kraken/test          Cheap private call (Balance).
    POST   /api/admin/kraken/poll          Manual OHLCV pull (force-refresh).
    DELETE /api/admin/kraken/disconnect    Wipe credentials + stop poller.
    POST   /api/admin/kraken/execution     Flip the execution toggle.
                                            Doctrine: defaults False; flip
                                            is audit-logged.

Doctrine:
    - Trading endpoints (AddOrder/CancelOrder) are intentionally not
      exposed by this router. Wiring them later is a separate change
      that goes through the existing promotion / dual-sign gate.
    - All flips of `execution_enabled` are logged to KRAKEN_AUDIT_LOG
      with operator email + timestamp + new state. Append-only.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import (
    KRAKEN_AUDIT_LOG,
    KRAKEN_CREDENTIALS,
)
from shared.credentials import encrypt, redact
from shared.crypto.kraken import (
    KrakenError,
    INTERNAL_TO_KRAKEN_PAIR,
    call_private,
    fetch_ohlc,
    get_active_keys,
    kraken_interval_for_tf,
    probe_scopes,
    to_internal_bar,
    to_kraken_pair,
)
from shared.technicals import _persist_bar, _recompute_snapshot


router = APIRouter(prefix="/admin/kraken", tags=["kraken"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── auto-poller task ────────────────────────

_POLLER_TASK: asyncio.Task | None = None
# 2026-06-18: split status into structured fields so the UI can
# distinguish a fresh-but-failed attempt from a stale error left over
# from a long-recovered Atlas pool pause. Without this split,
# `tick error: loop: ... connection pool paused` would stay visible
# on the Kraken card indefinitely even after the poller had been
# happily pulling bars for an hour.
#
#   ts                — wall-clock of the last COMPLETED tick attempt
#                       (success OR failure — anchors UI freshness)
#   last_success_ts   — wall-clock of the last tick that pushed bars
#   bars_pushed       — bars pushed in the most recent successful tick
#   error             — most recent failure reason (None when current)
_POLLER_LAST_TICK: dict = {
    "ts": None,
    "last_success_ts": None,
    "bars_pushed": 0,
    "error": None,
}


async def _poller_tick() -> None:
    """Single auto-poll iteration. Reads the configured pairs + tf from
    the singleton doc, pulls Kraken public OHLC for each, and pushes
    bars through the existing technicals ingest pipeline."""
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc or not doc.get("auto_poll_enabled", True):
        return

    pairs: list[str] = doc.get("pairs") or ["BTC/USD", "ETH/USD"]
    tf: str = doc.get("tf", "1h")
    interval = kraken_interval_for_tf(tf)

    bars_pushed = 0
    for symbol in pairs:
        kpair = to_kraken_pair(symbol)
        try:
            result = await fetch_ohlc(kpair, interval)
        except Exception as e:  # noqa: BLE001
            _POLLER_LAST_TICK["error"] = f"{symbol}: {e}"
            continue
        # Kraken returns {<altname>: [[ts,o,h,l,c,vwap,vol,count], ...], "last": ts}
        rows = next(
            (v for k, v in result.items() if k != "last" and isinstance(v, list)),
            None,
        )
        if not rows:
            continue
        # Last 60 bars per pair = enough overlap to absorb upstream revisions
        for row in rows[-60:]:
            bar = to_internal_bar(symbol, tf, row)
            bar["source"] = "kraken_pro"
            await _persist_bar(bar)
            bars_pushed += 1
        await _recompute_snapshot("kraken_pro", symbol.upper(), tf)
        await asyncio.sleep(0.4)  # gentle pacing across pairs

    _POLLER_LAST_TICK.update({
        "ts": _now_iso(),
        "bars_pushed": bars_pushed,
        # Per-tick contract: a successful tick clears prior errors AND
        # records its success time so the loop wrapper can suppress
        # transient errors that have since recovered.
        "error": _POLLER_LAST_TICK.get("error") if bars_pushed == 0 else None,
        "last_success_ts": _now_iso() if bars_pushed > 0 else _POLLER_LAST_TICK.get("last_success_ts"),
    })


async def _poller_loop() -> None:
    """Long-running task. Sleeps `poll_interval_seconds` (default 60) between ticks.

    Resilience pin (2026-06-18 — Prod "connection pool paused" UX bug):
    on a loop-level db error (Atlas pool pause, server selection
    timeout, etc.) we record the error AND bump `ts` so the operator
    can see that we attempted recently. A subsequent successful tick
    clears the error via _poller_tick's contract.
    """
    while True:
        try:
            doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
            if doc and doc.get("auto_poll_enabled", True):
                await _poller_tick()
            else:
                # Idle iteration — explicitly mark it so the operator
                # doesn't see a stale "error" from a long-recovered
                # pool pause. We're polling fine, just nothing to do.
                _POLLER_LAST_TICK.update({"ts": _now_iso(), "error": None})
            interval = (doc or {}).get("poll_interval_seconds", 60)
        except Exception as e:  # noqa: BLE001
            # Loop-level failure (typically a transient Atlas pool
            # pause or connection blip). Update ts so the UI shows
            # this attempt is fresh, and tag the error string with
            # the truncated exception type/message.
            _POLLER_LAST_TICK.update({
                "ts": _now_iso(),
                "error": f"loop: {e}",
            })
            interval = 60
        await asyncio.sleep(max(int(interval or 60), 15))


def start_poller_if_needed() -> None:
    global _POLLER_TASK
    if _POLLER_TASK and not _POLLER_TASK.done():
        return
    _POLLER_TASK = asyncio.create_task(_poller_loop(), name="kraken-auto-poller")


async def stop_poller() -> None:
    global _POLLER_TASK
    if _POLLER_TASK and not _POLLER_TASK.done():
        _POLLER_TASK.cancel()
        with suppress(asyncio.CancelledError):
            await _POLLER_TASK
    _POLLER_TASK = None


# ──────────────────────── audit log ────────────────────────

async def _audit(action: str, actor: str, payload: dict | None = None) -> None:
    await db[KRAKEN_AUDIT_LOG].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload or {},
    })


# ──────────────────────── pydantic models ────────────────────────

# Pair allowlist — only pairs we have a Kraken altname mapping for. We
# refuse to accept arbitrary symbols here so the operator can't typo
# their way into a silently-broken poller.
_ALLOWED_PAIRS = sorted(INTERNAL_TO_KRAKEN_PAIR.keys())
_ALLOWED_TFS = ("1m", "5m", "15m", "1h", "4h", "1d")


class ConnectIn(BaseModel):
    api_key: str = Field(..., min_length=20, max_length=200)
    private_key: str = Field(..., min_length=20, max_length=400, description="base64-encoded Kraken private key")
    pairs: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    tf: str = "1h"
    poll_interval_seconds: int = Field(default=60, ge=15, le=3600)

    @field_validator("pairs")
    @classmethod
    def _pairs_known(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one pair required")
        bad = [p for p in v if p.upper() not in _ALLOWED_PAIRS]
        if bad:
            raise ValueError(f"unknown pairs {bad}; supported: {_ALLOWED_PAIRS}")
        return [p.upper() for p in v]

    @field_validator("tf")
    @classmethod
    def _tf_known(cls, v: str) -> str:
        if v not in _ALLOWED_TFS:
            raise ValueError(f"tf must be one of {_ALLOWED_TFS}")
        return v


class ExecutionToggleIn(BaseModel):
    enabled: bool
    # Force the operator to type a literal confirmation phrase so this
    # flip can't happen via curl-by-accident.
    confirm: str = ""

    @field_validator("confirm")
    @classmethod
    def _confirm_required(cls, v: str) -> str:
        return v.strip()


# ──────────────────────── endpoints ────────────────────────

@router.post("/connect")
async def connect(body: ConnectIn, user: dict = Depends(get_current_user)):
    """Store encrypted keys, probe scopes, start the auto-poller."""
    # Probe BEFORE persisting — if the keys don't work, we don't store them.
    try:
        scopes = await probe_scopes(body.api_key, body.private_key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"scope probe failed: {e}") from e

    # Require at least query_funds to consider the keys valid.
    if not scopes.get("query_funds"):
        scope_errors = scopes.get("_errors", {}) or {}
        balance_err = scope_errors.get("query_funds", "")
        # Surface Kraken's actual complaint so the operator can tell apart
        # "wrong key", "bad permissions", "IP-restricted", "no funds account
        # tier", etc.
        kraken_says = f" — Kraken: {balance_err}" if balance_err else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"keys rejected: Balance probe denied{kraken_says}. "
                "Most common causes: (1) wrong key (recopy from Kraken — no "
                "spaces), (2) missing 'Query Funds' permission, "
                "(3) IP restriction not whitelisting this server, "
                "(4) key just created — Kraken takes ~30s to activate."
            ),
        )

    encrypted_priv = encrypt(body.private_key)
    now = _now_iso()
    doc = {
        "_id": "singleton",
        "public_key": body.api_key,
        "public_key_preview": redact(body.api_key, 6),
        "private_key_preview": redact(body.private_key, 4),
        "encrypted_private_key": encrypted_priv,
        "pairs": body.pairs,
        "tf": body.tf,
        "poll_interval_seconds": body.poll_interval_seconds,
        "auto_poll_enabled": True,
        "execution_enabled": False,  # doctrine — defaults off
        "scopes": {k: v for k, v in scopes.items() if not k.startswith("_")},
        "scope_errors": scopes.get("_errors", {}),
        "balance_preview": scopes.get("_balance_preview"),
        "last_nonce": 0,
        "created_at": now,
        "updated_at": now,
        "connected_by": user.get("email") or "operator",
    }
    await db[KRAKEN_CREDENTIALS].replace_one({"_id": "singleton"}, doc, upsert=True)
    await _audit("kraken_connect", user.get("email") or "operator", {"pairs": body.pairs, "tf": body.tf})

    # Kick the poller — first tick runs immediately.
    start_poller_if_needed()
    asyncio.create_task(_poller_tick())

    return _public_status(doc)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc:
        return {"connected": False, "execution_enabled": False, "poller_running": False}
    return _public_status(doc)


def _public_status(doc: dict) -> dict:
    """Shape the singleton doc for UI consumption. NEVER leak the
    encrypted private key or the plaintext public key past a redaction."""
    return {
        "connected": True,
        "public_key_preview": doc.get("public_key_preview"),
        "private_key_preview": doc.get("private_key_preview"),
        "pairs": doc.get("pairs", []),
        "tf": doc.get("tf"),
        "poll_interval_seconds": doc.get("poll_interval_seconds"),
        "auto_poll_enabled": doc.get("auto_poll_enabled", True),
        "execution_enabled": doc.get("execution_enabled", False),
        "scopes": doc.get("scopes", {}),
        "scope_errors": doc.get("scope_errors", {}),
        "balance_preview": doc.get("balance_preview"),
        "connected_by": doc.get("connected_by"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "poller_running": bool(_POLLER_TASK and not _POLLER_TASK.done()),
        "last_tick": _POLLER_LAST_TICK,
    }


@router.post("/reprobe")
async def reprobe(user: dict = Depends(get_current_user)):
    keys = await get_active_keys()
    if not keys:
        raise HTTPException(status_code=404, detail="no Kraken credentials stored")
    public_key, private_key = keys
    scopes = await probe_scopes(public_key, private_key)
    update = {
        "scopes": {k: v for k, v in scopes.items() if not k.startswith("_")},
        "scope_errors": scopes.get("_errors", {}),
        "balance_preview": scopes.get("_balance_preview"),
        "updated_at": _now_iso(),
    }
    await db[KRAKEN_CREDENTIALS].update_one({"_id": "singleton"}, {"$set": update})
    await _audit("kraken_reprobe", user.get("email") or "operator", {})
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    return _public_status(doc)


@router.post("/test")
async def test(user: dict = Depends(get_current_user)):
    """Cheap private call to confirm keys are still alive."""
    keys = await get_active_keys()
    if not keys:
        raise HTTPException(status_code=404, detail="no Kraken credentials stored")
    public_key, private_key = keys
    try:
        result = await call_private("/0/private/Balance", public_key, private_key)
    except KrakenError as e:
        raise HTTPException(status_code=502, detail=f"Kraken returned error: {e}") from e
    return {"ok": True, "assets": len(result), "called_at": _now_iso()}


@router.post("/poll")
async def manual_poll(user: dict = Depends(get_current_user)):
    """Force an immediate OHLC poll outside the scheduled interval."""
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="no Kraken credentials stored")
    await _poller_tick()
    return {"ok": True, "last_tick": _POLLER_LAST_TICK}


@router.delete("/disconnect")
async def disconnect(user: dict = Depends(get_current_user)):
    await db[KRAKEN_CREDENTIALS].delete_one({"_id": "singleton"})
    await stop_poller()
    await _audit("kraken_disconnect", user.get("email") or "operator", {})
    return {"ok": True}


@router.post("/execution")
async def toggle_execution(body: ExecutionToggleIn, user: dict = Depends(get_current_user)):
    """Flip the execution-allowed gate. Defaults off. Must be flipped
    explicitly with the literal confirmation phrase."""
    expected = "I authorize execution on Kraken" if body.enabled else "Disable execution"
    if body.confirm != expected:
        raise HTTPException(
            status_code=400,
            detail=f"confirmation phrase mismatch — expected: {expected!r}",
        )
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"})
    if not doc:
        raise HTTPException(status_code=404, detail="no Kraken credentials stored")
    await db[KRAKEN_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {"execution_enabled": body.enabled, "updated_at": _now_iso()}},
    )
    await _audit(
        "kraken_execution_toggle",
        user.get("email") or "operator",
        {"new_state": body.enabled},
    )
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    return _public_status(doc)


@router.get("/audit")
async def audit_log(
    limit: int = 50,
    _user: dict = Depends(get_current_user),
):
    rows = await db[KRAKEN_AUDIT_LOG].find({}, {"_id": 0}).sort("ts", -1).to_list(min(limit, 200))
    return {"items": rows, "count": len(rows)}
