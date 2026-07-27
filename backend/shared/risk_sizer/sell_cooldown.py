"""Post-sell cooldown (2026-07-28 operator directive).

Symptom: the moment a crypto SELL freed cash, the next fresh
mover-chasing BUY intent passed the balance check and redeployed it
within seconds — buying tops of 24h-mover pumps that then failed.
Doctrine: freed cash must COOL before the crypto lane may BUY again.
Knob: `crypto.post_sell_cooldown_min` in the risk-sizer policy
(default 30; 0 = off).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("risedual.sell_cooldown")

_FLAG_ID = "crypto_sell_cooldown"
_state: dict = {"mono": None, "iso": None, "symbol": None, "loaded": False}


def reset_for_tests() -> None:
    _state.update(mono=None, iso=None, symbol=None, loaded=True)


def note_crypto_sell(symbol: str) -> None:
    """Called by the exit monitor on every crypto SELL submit or
    externally observed close (operator selling on Kraken directly).
    Sync + in-memory (hot-path safe); the Atlas mirror is
    fire-and-forget for restart continuity only."""
    _state.update(
        mono=time.monotonic(),
        iso=datetime.now(timezone.utc).isoformat(),
        symbol=symbol,
        loaded=True,
    )
    logger.info("crypto sell noted %s — BUY cooldown armed", symbol)
    try:
        asyncio.get_running_loop().create_task(_mirror())
    except RuntimeError:
        pass


async def _mirror() -> None:
    try:
        from db import db  # noqa: WPS433
        await db["runtime_flags"].update_one(
            {"_id": _FLAG_ID},
            {"$set": {"last_sell_at": _state["iso"],
                      "symbol": _state["symbol"]}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sell cooldown mirror failed: %s", exc)


async def _ensure_loaded() -> None:
    """One-time restart-continuity load from the Atlas mirror."""
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        from db import db  # noqa: WPS433
        doc = await db["runtime_flags"].find_one({"_id": _FLAG_ID})
        raw = (doc or {}).get("last_sell_at")
        if raw:
            age_s = (
                datetime.now(timezone.utc) - datetime.fromisoformat(raw)
            ).total_seconds()
            if age_s >= 0:
                _state.update(
                    mono=time.monotonic() - age_s,
                    iso=raw,
                    symbol=(doc or {}).get("symbol"),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sell cooldown load failed: %s", exc)


async def cooldown_remaining_s(
    cooldown_min: float,
) -> tuple[float, Optional[str]]:
    """(remaining_seconds, last_sold_symbol) — (0, None) when clear."""
    if cooldown_min <= 0:
        return 0.0, None
    await _ensure_loaded()
    if _state["mono"] is None:
        return 0.0, None
    remaining = cooldown_min * 60.0 - (time.monotonic() - _state["mono"])
    if remaining <= 0:
        return 0.0, None
    return remaining, _state["symbol"]
