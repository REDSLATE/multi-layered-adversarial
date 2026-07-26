"""Option-chain data feed — Webull OpenAPI (contracts + snapshots).

Feeds the shared risk engine: resolves ONE concrete contract
(premium, bid/ask, open interest, Delta/Theta, DTE) for an options
intent that carries only underlying + direction. Fail-closed at every
step — missing credentials, missing OPRA options-quote entitlement,
empty chain, or all candidates failing the quality gates all yield
`contract: None` and a reason (NO_TRADE downstream).

Endpoints (same signed ApiClient the quotes module holds):
  GET /openapi/instrument/option/contracts   chain (static, cached 15m)
  GET /openapi/market-data/option/snapshot   premium + greeks + OI
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger("risedual.options.chain")

_CHAIN_TTL_S = 900.0
_chain_cache: dict[str, dict[str, Any]] = {}   # underlying → {at, rows}

_SNAPSHOT_BATCH = 20
_STRIKES_NEAR_MONEY = 14
_MAX_CHAIN_PAGES = 3


def reset_for_tests() -> None:
    _chain_cache.clear()


def occ_symbol(underlying: str, expire_date: str, option_type: str,
               strike: float) -> str:
    """Compact OCC-style symbol matching Webull chain symbols
    (AAPL260828C00340000)."""
    d = datetime.strptime(str(expire_date)[:10], "%Y-%m-%d")
    cp = "C" if str(option_type).upper().startswith("C") else "P"
    return (f"{underlying.upper().strip()}{d.strftime('%y%m%d')}{cp}"
            f"{int(round(float(strike) * 1000)):08d}")


def _api_client():
    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
    c = get_quotes_client()
    if c is None:
        return None
    return c._data.option_market_data.client  # noqa: SLF001


def _fetch_chain_rows(underlying: str) -> list[dict]:
    """All LISTING US_OPTION contracts for the underlying (paginated)."""
    from webull.core.request import ApiRequest  # noqa: WPS433
    client = _api_client()
    if client is None:
        raise RuntimeError("no Webull quotes client (credentials missing)")
    rows: list[dict] = []
    last_id: Optional[str] = None
    for _page in range(_MAX_CHAIN_PAGES):
        req = ApiRequest(
            "/openapi/instrument/option/contracts",
            version="v2", method="GET", query_params={},
        )
        req.add_query_param("category", "US_OPTION")
        req.add_query_param("underlying_symbols", underlying.upper())
        req.add_query_param("status", "LISTING")
        req.add_query_param("page_size", "1000")
        if last_id:
            req.add_query_param("last_instrument_id", last_id)
        res = client.get_response(req)
        body = res.json() if hasattr(res, "json") else res
        page = body if isinstance(body, list) else (body or {}).get("data") or []
        if not isinstance(page, list) or not page:
            break
        rows.extend(r for r in page if isinstance(r, dict))
        if len(page) < 1000:
            break
        last_id = str(page[-1].get("instrument_id") or "") or None
        if not last_id:
            break
    return rows


def _fetch_snapshots(symbols: list[str]) -> list[dict]:
    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
    c = get_quotes_client()
    if c is None:
        raise RuntimeError("no Webull quotes client (credentials missing)")
    out: list[dict] = []
    for i in range(0, len(symbols), _SNAPSHOT_BATCH):
        batch = symbols[i:i + _SNAPSHOT_BATCH]
        res = c._data.option_market_data.get_option_snapshot(  # noqa: SLF001
            ",".join(batch), "US_OPTION",
        )
        body = res.json() if hasattr(res, "json") else res
        rows = body if isinstance(body, list) else (body or {}).get("data") or []
        if isinstance(rows, list):
            out.extend(r for r in rows if isinstance(r, dict))
    return out


def _fetch_spot(underlying: str) -> Optional[float]:
    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
    c = get_quotes_client()
    if c is None:
        return None
    res = c._data.market_data.get_snapshot([underlying.upper()], "US_STOCK")  # noqa: SLF001
    body = res.json() if hasattr(res, "json") else res
    rows = body if isinstance(body, list) else (body or {}).get("data") or []
    if not rows:
        return None
    row = rows[0] or {}
    for key in ("price", "close", "pre_close"):
        try:
            v = float(row.get(key) or 0.0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def _num(row: dict, key: str) -> Optional[float]:
    try:
        v = row.get(key)
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _dte(expiration: str) -> Optional[float]:
    try:
        d = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
        return float((d - date.today()).days)
    except ValueError:
        return None


def _chain(underlying: str) -> list[dict]:
    key = underlying.upper()
    cached = _chain_cache.get(key)
    if cached and time.monotonic() - cached["at"] < _CHAIN_TTL_S:
        return cached["rows"]
    rows = _fetch_chain_rows(key)
    _chain_cache[key] = {"at": time.monotonic(), "rows": rows}
    return rows


def _resolve_sync(underlying: str, action: str, pol: dict) -> dict:
    """Pick the single best contract for the intent. Returns
    {contract: dict|None, reason, spot, expiration, considered,
    rejections}."""
    from shared.risk_sizer import options_gate  # noqa: WPS433

    out: dict[str, Any] = {"contract": None, "underlying": underlying.upper(),
                           "spot": None, "expiration": None,
                           "considered": 0, "rejections": []}
    option_type = "PUT" if (action or "").upper() == "SHORT" else "CALL"

    try:
        chain = _chain(underlying)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"chain_fetch_failed: {exc}"
        return out
    if not chain:
        out["reason"] = "empty_chain"
        return out

    spot = _fetch_spot(underlying)
    if not spot:
        out["reason"] = "no_underlying_quote"
        return out
    out["spot"] = spot

    min_dte, max_dte = float(pol["min_dte"]), float(pol["max_dte"])
    eligible = []
    for row in chain:
        if (row.get("option_type") or "").upper() != option_type:
            continue
        if (row.get("tradable_status") or "OC") != "OC":
            continue
        dte = _dte(str(row.get("expiration_date") or ""))
        strike = _num(row, "strike_price")
        if dte is None or strike is None:
            continue
        if not (min_dte <= dte <= max_dte):
            continue
        eligible.append({**row, "_dte": dte, "_strike": strike})
    if not eligible:
        out["reason"] = "no_contracts_in_dte_window"
        return out

    # ONE expiration: closest to the middle of the DTE window.
    target_dte = (min_dte + max_dte) / 2.0
    expiration = min(
        {r["expiration_date"] for r in eligible},
        key=lambda e: abs((_dte(e) or 0.0) - target_dte),
    )
    out["expiration"] = expiration
    at_exp = [r for r in eligible if r["expiration_date"] == expiration]
    at_exp.sort(key=lambda r: abs(r["_strike"] - spot))
    picks = at_exp[:_STRIKES_NEAR_MONEY]
    out["considered"] = len(picks)

    try:
        snaps = {s.get("symbol"): s for s in _fetch_snapshots([r["symbol"] for r in picks])}
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"snapshot_fetch_failed: {exc}"
        return out

    target_delta = float(pol.get("target_abs_delta") or 0.50)
    candidates = []
    for row in picks:
        snap = snaps.get(row["symbol"])
        if not snap:
            out["rejections"].append({"symbol": row["symbol"], "reason": "no_snapshot"})
            continue
        bid, ask = _num(snap, "bid"), _num(snap, "ask")
        mid = (bid + ask) / 2.0 if bid and ask else None
        od = {
            "symbol": row["symbol"],
            "underlying": underlying.upper(),
            "option_type": option_type,
            "strike_price": row["_strike"],
            "expiration": str(expiration),
            "dte": row["_dte"],
            "premium": mid,
            "bid": bid,
            "ask": ask,
            "open_interest": _num(snap, "open_interest"),
            "delta": _num(snap, "delta"),
            "theta": _num(snap, "theta"),
            "gamma": _num(snap, "gamma"),
            "imp_vol": _num(snap, "imp_vol"),
            "multiplier": _num(row, "multiplier") or 100.0,
        }
        chk = options_gate.check({"option": od}, pol)
        if not chk["ok"]:
            out["rejections"].append({"symbol": row["symbol"], "reason": chk["reason"]})
            continue
        candidates.append(od)
    if not candidates:
        out["reason"] = "no_contract_passes_quality_gates"
        return out

    best = min(candidates, key=lambda c: abs(abs(c["delta"]) - target_delta))
    out["contract"] = best
    out["reason"] = "resolved"
    logger.info(
        "options resolve %s %s → %s prem=%.2f Δ=%.2f OI=%.0f dte=%.0f",
        underlying, option_type, best["symbol"], best["premium"],
        best["delta"], best["open_interest"], best["dte"],
    )
    return out


async def resolve_contract(underlying: str, action: str, pol: dict) -> dict:
    """Async wrapper — the SDK calls are blocking HTTPS."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, _resolve_sync, underlying, action, dict(pol),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("options resolve failed %s: %s", underlying, exc)
        return {"contract": None, "reason": f"resolver_error: {exc}",
                "underlying": underlying.upper()}
