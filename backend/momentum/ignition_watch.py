"""Ignition Watch (2026-08-04 operator directive).

The momentum scanner only reads symbols already in the BUY universe —
an igniting off-list coin (the ICNT class) is invisible until the
15-min universe refresh happens to admit it, by which time the move
is stale and the chase guard correctly blocks it.

This closes the discovery gap: ONE bulk public Ticker call per scan
cycle over ALL online Kraken USD pairs, ranked by the per-minute
delta of 24h dollar volume between sweeps. Fresh volume inflow with
positive price movement = ignition. Top-N candidates feed the
scanner's crypto lane; eligibility rules + the $5/trade cap still
decide what may actually trade.

First sweep records a baseline and returns nothing (delta needs two
samples). Fail-soft everywhere — the universe scan never stalls on a
sweep failure.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("risedual.ignition_watch")

TICKER_URL = "https://api.kraken.com/0/public/Ticker"

# fiat / stables — huge volume, never "ignition"
_EXCLUDED_BASES = {
    "USDT", "USDC", "DAI", "TUSD", "PYUSD", "USDS", "USDG", "EURT",
    "EUR", "GBP", "AUD", "CHF", "JPY", "CAD", "USD",
}

_prev: dict = {"at": None, "rows": {}}
_last: dict = {"at": 0.0, "cands": None}
_MIN_SWEEP_GAP_S = 30.0


def reset_for_tests() -> None:
    _prev.update(at=None, rows={})
    _last.update(at=0.0, cands=None)


def compute_candidates(
    rows: dict[str, tuple[float, float]],
    prev_rows: dict[str, tuple[float, float]],
    elapsed_min: float,
    *, top_n: int, min_vol_usd_min: float,
) -> list[dict]:
    """Pure ranking: rows/prev_rows are base → (dollar_vol_24h, last).
    Candidates need positive volume-delta rate AND positive price move."""
    elapsed_min = max(0.5, elapsed_min)
    out = []
    for base, (dvol, last) in rows.items():
        p = prev_rows.get(base)
        if not p or last <= 0:
            continue
        rate = (dvol - p[0]) / elapsed_min
        chg = (last / p[1] - 1.0) if p[1] > 0 else 0.0
        if rate < min_vol_usd_min or chg <= 0:
            continue
        out.append({
            "symbol": f"{base}/USD",
            "vol_rate_usd_min": round(rate),
            "price_change_pct": round(chg * 100.0, 3),
            "last_price": last,
        })
    out.sort(key=lambda c: c["vol_rate_usd_min"], reverse=True)
    return out[:top_n]


async def _fetch_all_tickers() -> Optional[dict]:
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(TICKER_URL)
        r.raise_for_status()
        body = r.json()
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body.get("result") or {}


async def sweep(*, top_n: int = 5,
                min_vol_usd_min: float = 10_000.0) -> list[dict]:
    now = time.monotonic()
    if _last["cands"] is not None and now - _last["at"] < _MIN_SWEEP_GAP_S:
        return _last["cands"]
    from shared.crypto.kraken_pair_sync import get_usd_pairs  # noqa: WPS433
    pairs = await get_usd_pairs()
    if not pairs:
        return []
    pair_to_base = {v["pair"]: base for base, v in pairs.items()}
    try:
        result = await _fetch_all_tickers()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ignition ticker sweep failed: %s", exc)
        return []
    rows: dict[str, tuple[float, float]] = {}
    for pair_key, t in result.items():
        base = pair_to_base.get(pair_key)
        if not base or base in _EXCLUDED_BASES:
            continue
        try:
            vol24 = float(t["v"][1])
            vwap24 = float(t["p"][1])
            last = float(t["c"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if last <= 0 or vol24 <= 0:
            continue
        rows[base] = (vol24 * (vwap24 if vwap24 > 0 else last), last)

    prev_rows, prev_at = _prev["rows"], _prev["at"]
    _prev.update(rows=rows, at=now)
    cands: list[dict] = []
    if prev_rows and prev_at is not None:
        cands = compute_candidates(
            rows, prev_rows, (now - prev_at) / 60.0,
            top_n=top_n, min_vol_usd_min=min_vol_usd_min)
        if cands:
            logger.info("ignition sweep: %s", ", ".join(
                f"{c['symbol']} ${c['vol_rate_usd_min']}/min "
                f"+{c['price_change_pct']}%" for c in cands))
    _last.update(at=now, cands=cands)
    return cands
