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
    unreachable the fetcher RAISES so the refresher's outer
    try/except catches it and retains the last-good universe.

    2026-07-15 iter-30 P4b: never silently truncate. A 429 or
    partial-success (Kraken returns HTTP 200 but with `error[]`
    non-empty and a truncated `result{}`) MUST fail the whole
    ticker fetch, not return a partial dict pretending to be
    complete. The refresh-safeguard depends on "empty or full,
    never partial" — a truncated ranking is worse than no
    refresh at all because the operator wouldn't know their
    "top gainers" was built from 60% of the real universe.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import httpx

logger = logging.getLogger("risedual.universe.kraken_movers")

_KRAKEN_BASE = "https://api.kraken.com"
_USER_AGENT = "risedual-mission-control/1.0"

# Kraken public rate limit is documented as 15 calls / 10s per IP.
# BATCH controls how many pairs we pack into one Ticker call —
# smaller = fewer 429s + smaller blast-radius per failure. 25 keeps
# us well under URL-length limits with room for very long pair
# names (some Kraken listings run to 12+ chars per pair).
BATCH = 25

# Retry budget: at most 2 retries per chunk. On 429, honor
# `Retry-After` if present, else fall back to a jittered backoff.
MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE_SEC = 3.0
DEFAULT_BACKOFF_JITTER_SEC = 2.0


class KrakenBatchError(RuntimeError):
    """Raised when a Ticker batch could not be resolved cleanly
    (429 exhausted, network error, or Kraken partial-success shape
    with a non-empty `error[]`). Whoever catches this MUST retain
    the last-good universe rather than publish partial data."""


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


def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """Parse a `Retry-After` header. Kraken usually sends seconds
    (delta-seconds) but HTTP allows an HTTP-date form too — we
    only support the seconds form. Returns None on unparseable."""
    if not header_value:
        return None
    try:
        v = float(header_value.strip())
    except (TypeError, ValueError):
        return None
    if v < 0 or v > 60:
        # Sanity clamp — a 5-min Retry-After would starve the
        # refresh cycle entirely; treat implausible values as
        # missing and fall back to our own backoff schedule.
        return None
    return v


async def _fetch_ticker_chunk(
    client: httpx.AsyncClient, chunk: list[str], attempt: int,
) -> dict:
    """One `/Ticker` call. Returns Kraken's `result` dict on clean
    success. Raises `KrakenBatchError` on any of:
      * HTTP 4xx/5xx after `raise_for_status`
      * `data['error']` non-empty
      * JSON decode error

    Callers loop with backoff around this and eventually raise
    upward if the retry budget is exhausted."""
    r = await client.get(
        f"{_KRAKEN_BASE}/0/public/Ticker",
        params={"pair": ",".join(chunk)},
        headers={"User-Agent": _USER_AGENT},
    )
    if r.status_code == 429:
        retry_after = _parse_retry_after(r.headers.get("Retry-After"))
        raise KrakenBatchError(
            f"HTTP 429 rate-limited (attempt {attempt}, "
            f"retry_after={retry_after})",
        ).with_traceback(None)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError as exc:
        raise KrakenBatchError(f"JSON decode failed: {exc}") from exc
    errors = data.get("error") or []
    if errors:
        # Kraken's partial-success shape — HTTP 200 but SOME pairs
        # in the batch failed. Reject the whole chunk; a truncated
        # ranking is worse than no refresh at all.
        raise KrakenBatchError(
            f"Kraken returned non-empty error[]: {errors!r} "
            f"(result rows={len(data.get('result') or {})} "
            f"vs requested={len(chunk)})",
        )
    result = data.get("result")
    if not isinstance(result, dict):
        raise KrakenBatchError(f"unexpected result shape: {type(result).__name__}")
    # Additional safety net: if Kraken silently drops pairs
    # WITHOUT populating error[], count-compare + reject.
    if len(result) < len(chunk):
        raise KrakenBatchError(
            f"silent truncation: requested {len(chunk)} pairs, "
            f"got {len(result)} — refusing to publish partial batch",
        )
    return result


async def _fetch_all_tickers(altnames: list[str]) -> dict:
    """Bulk /Ticker in BATCH-sized chunks. RAISES on any chunk
    that can't be resolved after MAX_RETRIES — never returns a
    truncated dict.

    Retry policy:
        * On 429, honor `Retry-After` header if present (clamped
          to [0, 60] sec). Otherwise use
          `DEFAULT_BACKOFF_BASE_SEC + jitter[0, JITTER_SEC]`.
        * On other errors (network / JSON), backoff without
          Retry-After — jitter is applied regardless so multiple
          pods don't synchronise their retries.
    """
    if not altnames:
        return {}
    out: dict = {}
    async with httpx.AsyncClient(timeout=8.0) as client:
        for i in range(0, len(altnames), BATCH):
            chunk = altnames[i:i + BATCH]
            last_exc: Optional[BaseException] = None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    result = await _fetch_ticker_chunk(client, chunk, attempt)
                    out.update(result)
                    last_exc = None
                    break
                except KrakenBatchError as exc:
                    last_exc = exc
                    if attempt >= MAX_RETRIES:
                        break
                    # Extract Retry-After if this was a 429.
                    retry_after: Optional[float] = None
                    msg = str(exc)
                    if "retry_after=" in msg:
                        # Best-effort parse from the exception message.
                        try:
                            frag = msg.split("retry_after=", 1)[1].split(")", 1)[0]
                            retry_after = float(frag) if frag != "None" else None
                        except (ValueError, IndexError):
                            retry_after = None
                    if retry_after is None:
                        # Jittered backoff — different pods desync.
                        retry_after = (
                            DEFAULT_BACKOFF_BASE_SEC
                            + random.uniform(0.0, DEFAULT_BACKOFF_JITTER_SEC)
                        )
                    logger.warning(
                        "kraken Ticker chunk starting %s failed "
                        "(attempt %d/%d): %s — backing off %.2fs",
                        chunk[0], attempt + 1, MAX_RETRIES + 1, exc, retry_after,
                    )
                    await asyncio.sleep(retry_after)
                except httpx.HTTPError as exc:
                    # Network-level failure. Same retry loop.
                    last_exc = exc
                    if attempt >= MAX_RETRIES:
                        break
                    backoff = (
                        DEFAULT_BACKOFF_BASE_SEC
                        + random.uniform(0.0, DEFAULT_BACKOFF_JITTER_SEC)
                    )
                    logger.warning(
                        "kraken Ticker chunk starting %s network error "
                        "(attempt %d/%d): %s — backing off %.2fs",
                        chunk[0], attempt + 1, MAX_RETRIES + 1, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
            if last_exc is not None:
                # Exhausted retries for this chunk. Do NOT return
                # partial `out` — raise so the refresher's outer
                # try/except catches and last-good is retained.
                raise KrakenBatchError(
                    f"kraken ticker batch failed after {MAX_RETRIES + 1} "
                    f"attempts (chunk starting {chunk[0]}, "
                    f"size={len(chunk)}): {last_exc}",
                ) from last_exc
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
        # Live spread from the ticker (2026-07-28 operator fix #4):
        # `b`/`a` are [price, whole_lot_vol, lot_vol]. Stamped so the
        # refresher can drop chronically wide-spread pairs BEFORE the
        # brains burn intents on them.
        spread_bps = None
        try:
            bid = float((row.get("b") or [0])[0])
            ask = float((row.get("a") or [0])[0])
            if bid > 0 and ask >= bid:
                spread_bps = round((ask - bid) / ((ask + bid) / 2.0) * 10_000.0, 2)
        except (TypeError, ValueError, IndexError):
            pass
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
            "spread_bps": spread_bps,
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
