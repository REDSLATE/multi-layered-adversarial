"""Crypto BUY allowlist (2026-07-28 operator directive).

"Allowlist-only BUY universe": the 24h-movers list becomes
watch-only — the crypto lane may only BUY symbols the operator has
explicitly whitelisted. SELLs / exits are NEVER gated (you can
always leave a position).

Storage: runtime_flags._id=crypto_buy_allowlist
  {enabled: bool, symbols: ["BTC/USD", ...], updated_by, updated_at}
Managed via GET/PUT /api/admin/universe/crypto-buy-allowlist.
Missing doc → enabled with the liquid-majors default below.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("risedual.buy_allowlist")

FLAG_ID = "crypto_buy_allowlist"
DEFAULT_ALLOWLIST = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD",
    "ADA/USD", "DOGE/USD", "LTC/USD", "LINK/USD",
]
_CACHE_TTL_S = 30.0
_cache: dict = {"at": 0.0, "doc": None}

# Kraken internal base-asset codes → canonical tickers, so
# "SOON/USD", "SOONUSD" and Kraken's pair name can never drift into
# distinct allowlist entries.
_KRAKEN_BASE_ALIASES = {
    "XBT": "BTC", "XXBT": "BTC", "XETH": "ETH", "XDG": "DOGE",
    "XXDG": "DOGE", "XXRP": "XRP", "XLTC": "LTC", "XXLM": "XLM",
    "XXMR": "XMR", "XZEC": "ZEC", "XETC": "ETC", "XREP": "REP",
}
_QUOTE_SUFFIXES = ("ZUSD", "USDT", "USDC", "USD")


def normalize_crypto_symbol(symbol: str) -> str:
    """Canonicalize any crypto symbol form to BASE/USD.
    Accepts: SOON, SOON/USD, SOON-USD, SOONUSD, CRYPTO:SOON-USD,
    Kraken internals (XBT, XXBTZUSD, …). Empty/garbage → ""."""
    s = (symbol or "").strip().upper().removeprefix("CRYPTO:")
    if "/" in s or "-" in s:
        base = s.replace("-", "/").split("/", 1)[0]
    else:
        base = s
        for suf in _QUOTE_SUFFIXES:
            if base.endswith(suf) and len(base) > len(suf):
                base = base[: -len(suf)]
                break
    base = _KRAKEN_BASE_ALIASES.get(base, base)
    return f"{base}/USD" if base else ""


def invalidate_cache() -> None:
    _cache.update(at=0.0, doc=None)


def reset_for_tests() -> None:
    invalidate_cache()


async def get_allowlist() -> dict:
    now = time.monotonic()
    if _cache["doc"] is not None and now - _cache["at"] < _CACHE_TTL_S:
        return _cache["doc"]
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one({"_id": FLAG_ID}, {"_id": 0})
    if not doc:
        doc = {"enabled": True, "symbols": list(DEFAULT_ALLOWLIST),
               "source": "default"}
    doc.setdefault("enabled", True)
    doc["symbols"] = sorted({
        normalize_crypto_symbol(s) for s in (doc.get("symbols") or [])
    } - {""})
    _cache.update(at=now, doc=doc)
    return doc


async def buy_allowed(symbol: str) -> tuple[bool, dict]:
    """(allowed, allowlist_doc). Disabled list → everything allowed."""
    al = await get_allowlist()
    if not al.get("enabled"):
        return True, al
    return normalize_crypto_symbol(symbol) in set(al["symbols"]), al
