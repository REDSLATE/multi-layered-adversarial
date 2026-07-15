"""Kraken crypto movers — top 24h gainers/losers + highest-liquidity
USD pairs.

Kraken's public endpoints:
    /0/public/AssetPairs  → all tradable pairs (universe)
    /0/public/Ticker      → per-pair 24h stats incl. `p` (VWAP),
                            `v` (volume), `c` (last), `o` (open)

Doctrine (2026-07-15, iter-30 P4):
    Crypto is 24/7 — there's no "session open" to compute
    session-relative change against, so we use Kraken's 24-hour
    change (last vs 24h open) as the mover signal. Highest-
    liquidity supplement filled from `v` (24h volume).

    All fetches unauthenticated — no keys touched. If Kraken is
    unreachable the fetcher returns [] and the refresher retains
    the previous universe.

Returns normalized shape (matches `webull_movers._row_to_mover`):

    [{
      "canonical_symbol": "BTC/USD",
      "broker_instrument_id": "XXBTZUSD",   # Kraken's canonical
      "change_pct": 3.42,
      "volume": 1234567.0,                  # 24h volume
      "price": 68123.4,
      "source_reason": "top_gainer" | "top_loser" | "high_liquidity",
    }, ...]
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("risedual.universe.kraken_movers")

_KRAKEN_BASE = "https://api.kraken.com"
_USER_AGENT = "risedual-mission-control/1.0"


def _altname_to_canonical(altname: str, wsname: Optional[str]) -> Optional[str]:
    """Kraken altname `XBTUSD` → canonical `BTC/USD`.

    Prefers `wsname` (Kraken's "XBT/USD" websocket-friendly name)
    when present, otherwise best-effort splits the altname on the
    quote currency. Returns None if we can't confidently canonicalize.
    """
    if wsname and "/" in wsname:
        # Kraken uses XBT for Bitcoin; canonicalize to BTC.
        canonical = wsname.upper().replace("XBT/", "BTC/")
        return canonical
    if not altname:
        return None
    u = altname.upper()
    # Try common USD quote suffixes.
    for suffix in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
        if u.endswith(suffix) and len(u) > len(suffix):
            base = u[:-len(suffix)]
            if base == "XBT":
                base = "BTC"
            return f"{base}/{suffix}"
    return None


async def _fetch_asset_pairs() -> dict:
    """Kraken's tradable pairs registry — this is the AUTHORITATIVE
    Kraken-side universe. Returns `{altname: pair_meta}` or {} on
    error."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_KRAKEN_BASE}/0/public/AssetPairs",
                headers={"User-Agent": _USER_AGENT},
            )
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("kraken AssetPairs fetch failed: %s", exc)
        return {}
    if data.get("error"):
        # Partial errors — data.result may still be present.
        pass
    return (data.get("result") or {})


async def _fetch_all_tickers(altnames: list[str]) -> dict:
    """Bulk /Ticker call. Kraken accepts up to ~50-60 pairs per call
    based on their gateway; we batch conservatively."""
    if not altnames:
        return {}
    out: dict = {}
    BATCH = 40
    async with httpx.AsyncClient(timeout=8.0) as client:
        for i in range(0, len(altnames), BATCH):
            chunk = altnames[i:i + BATCH]
            try:
                r = await client.get(
                    f"{_KRAKEN_BASE}/0/public/Ticker",
                    params={"pair": ",".join(chunk)},
                    headers={"User-Agent": _USER_AGENT},
                )
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "kraken Ticker fetch failed for chunk starting %s: %s",
                    chunk[0], exc,
                )
                continue
            for kkey, row in (data.get("result") or {}).items():
                out[kkey] = row
    return out


async def fetch_crypto_movers(
    top_gainers: int = 20,
    top_losers: int = 20,
    high_liquidity: int = 20,
    quote_currency: str = "USD",
) -> list[dict]:
    """Compute crypto movers from Kraken public endpoints.

    Filters to USD-quoted pairs by default (matches the current
    operator universe — the pod isn't set up to hold multi-quote
    stables today). Returns a UNION of the three source categories
    with dedupe left to the caller.
    """
    pairs = await _fetch_asset_pairs()
    if not pairs:
        return []

    # Keep only USD-quoted, live-status pairs.
    keep_altnames: list[str] = []
    meta_by_altname: dict[str, dict] = {}
    for kkey, p in pairs.items():
        if not isinstance(p, dict):
            continue
        altname = (p.get("altname") or kkey or "").upper()
        wsname = p.get("wsname")
        status = (p.get("status") or "").lower()
        quote = (p.get("quote") or "").upper()
        # Kraken uses ZUSD as the canonical fiat asset code.
        quote_matches = (
            quote == quote_currency
            or quote == "Z" + quote_currency
            or altname.endswith(quote_currency)
        )
        if not quote_matches or status not in {"online", "", "reduce_only"}:
            continue
        keep_altnames.append(altname)
        meta_by_altname[altname] = {
            "altname": altname,
            "wsname": wsname,
            "canonical_pair_id": kkey,
        }

    if not keep_altnames:
        return []

    tickers = await _fetch_all_tickers(keep_altnames)
    if not tickers:
        return []

    # Parse each ticker row into a mover candidate.
    candidates: list[dict] = []
    for kkey, row in tickers.items():
        if not isinstance(row, dict):
            continue
        # `o` is 24h opening price; `c` is last-trade [price, vol].
        try:
            open_24h = float(row.get("o") or 0.0)
            last_str = (row.get("c") or [0, 0])[0]
            last = float(last_str)
        except (TypeError, ValueError, IndexError):
            continue
        if open_24h <= 0 or last <= 0:
            continue
        change_pct = (last - open_24h) / open_24h * 100.0
        try:
            # `v` = [today_vol, last_24h_vol]; take 24h.
            vol_str = (row.get("v") or [0, 0])[1]
            volume = float(vol_str)
        except (TypeError, ValueError, IndexError):
            volume = 0.0
        # kkey may be Kraken's canonical (XXBTZUSD) OR the altname
        # (BTCUSD) depending on how it was queried. Look up meta by
        # matching either.
        meta = None
        for alt, m in meta_by_altname.items():
            if kkey == alt or kkey == m["canonical_pair_id"]:
                meta = m
                break
        if meta is None:
            continue
        canonical = _altname_to_canonical(meta["altname"], meta["wsname"])
        if not canonical:
            continue
        candidates.append({
            "canonical_symbol": canonical,
            "broker_instrument_id": meta["canonical_pair_id"],
            "change_pct": round(change_pct, 4),
            "volume": volume,
            "price": last,
            "_altname": meta["altname"],
        })

    if not candidates:
        return []

    # Sort three ways, tag each with source_reason.
    by_change_desc = sorted(candidates, key=lambda r: r["change_pct"], reverse=True)
    gainers = by_change_desc[:top_gainers]
    losers = sorted(candidates, key=lambda r: r["change_pct"])[:top_losers]
    liquid = sorted(candidates, key=lambda r: r["volume"], reverse=True)[:high_liquidity]

    out: list[dict] = []
    for r in gainers:
        row = dict(r)
        row.pop("_altname", None)
        row["source_reason"] = "top_gainer"
        out.append(row)
    for r in losers:
        row = dict(r)
        row.pop("_altname", None)
        row["source_reason"] = "top_loser"
        out.append(row)
    for r in liquid:
        row = dict(r)
        row.pop("_altname", None)
        row["source_reason"] = "high_liquidity"
        out.append(row)
    return out
