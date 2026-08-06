"""Kraken Equities API capability probe (2026-08 unified-broker study).

READ-ONLY. Determines empirically whether the operator's existing
Kraken key can discover, query, and (dry-run) submit TRADITIONAL US
equity orders — the prerequisite before any Webull→Kraken migration.
No order is ever placed: submission checks use `validate=true`.

Capabilities probed:
  crypto_private      — Balance works (baseline: key + crypto scope OK)
  equity_instruments  — public AssetPairs exposes equity/tokenized pairs
  equity_balances     — private Balance(Ex) shows equity-looking holdings
  equity_order_dryrun — AddOrder validate=true on an equity pair:
                          EOrder:*  → permission GRANTED (shape rejected)
                          EAPI/EGeneral permission → scope MISSING
                          EQuery:Unknown asset pair → pair not API-tradable
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from shared.crypto.kraken import (
    KRAKEN_BASE, USER_AGENT, KrakenError, call_private, get_active_keys,
)

logger = logging.getLogger("risedual.kraken_equities_probe")

# Candidate discovery queries — Kraken's docs require `asset_class`
# for non-crypto pairs on AddOrder; discovery param naming is probed
# across the plausible variants.
_DISCOVERY_QUERIES = [
    ("asset_class=equity", {"asset_class": "equity"}),
    ("asset_class=tokenized_asset", {"asset_class": "tokenized_asset"}),
    ("aclass_base=equity", {"aclass_base": "equity"}),
]
_EQUITY_TICKERS = ("AAPL", "TSLA", "SPY", "MSFT", "NVDA")


async def _public_get(path: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{KRAKEN_BASE}{path}", params=params,
                             headers={"User-Agent": USER_AGENT})
    data = r.json()
    if data.get("error"):
        raise KrakenError(data["error"], status=r.status_code)
    return data.get("result", {})


def _equity_like(pairs: dict) -> list[str]:
    hits = []
    for name, meta in pairs.items():
        base = str((meta or {}).get("base") or "").upper()
        wsname = str((meta or {}).get("wsname") or "").upper()
        aclass = str((meta or {}).get("aclass_base") or "").lower()
        if aclass in ("equity", "tokenized_asset"):
            hits.append(name)
        elif any(t == base or wsname.startswith(f"{t}/") or wsname.startswith(f"{t}X/")
                 for t in _EQUITY_TICKERS):
            hits.append(name)
    return hits


async def run_equities_probe() -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": {},
        "verdict": None,
    }
    caps = out["capabilities"]

    keys = await get_active_keys()
    if not keys:
        return {**out, "ok": False,
                "error": "no active Kraken keys — connect Kraken first"}
    pub, priv = keys

    # 1. crypto baseline
    try:
        await call_private("/0/private/Balance", pub, priv, {})
        caps["crypto_private"] = {"status": "supported"}
    except Exception as e:  # noqa: BLE001
        caps["crypto_private"] = {"status": "error", "detail": str(e)[:200]}

    # 2. equity instrument discovery (public)
    discovered: list[str] = []
    disc_detail = {}
    for label, params in _DISCOVERY_QUERIES:
        try:
            pairs = await _public_get("/0/public/AssetPairs", params)
            hits = _equity_like(pairs) if pairs else []
            disc_detail[label] = {"pairs_returned": len(pairs),
                                  "equity_like": len(hits),
                                  "samples": hits[:5]}
            discovered.extend(hits)
        except Exception as e:  # noqa: BLE001
            disc_detail[label] = {"error": str(e)[:160]}
    # default catalog too — some listings appear without params
    try:
        pairs = await _public_get("/0/public/AssetPairs", {})
        hits = _equity_like(pairs)
        disc_detail["default_catalog"] = {"pairs_returned": len(pairs),
                                          "equity_like": len(hits),
                                          "samples": hits[:5]}
        discovered.extend(hits)
    except Exception as e:  # noqa: BLE001
        disc_detail["default_catalog"] = {"error": str(e)[:160]}
    discovered = sorted(set(discovered))
    caps["equity_instruments"] = {
        "status": "supported" if discovered else "not_found",
        "n_discovered": len(discovered),
        "samples": discovered[:10],
        "queries": disc_detail,
    }

    # 3. equity balances (private)
    try:
        bal = await call_private("/0/private/BalanceEx", pub, priv, {})
    except Exception:  # noqa: BLE001
        try:
            bal = await call_private("/0/private/Balance", pub, priv, {})
        except Exception as e:  # noqa: BLE001
            bal = None
            caps["equity_balances"] = {"status": "error", "detail": str(e)[:160]}
    if bal is not None:
        eq_assets = [a for a in bal
                     if any(t in a.upper() for t in _EQUITY_TICKERS)
                     or a.upper().endswith("X")]
        caps["equity_balances"] = {
            "status": "visible" if eq_assets else "none_held",
            "equity_like_assets": eq_assets[:10],
            "n_assets_total": len(bal),
        }

    # 4. equity order dry-run (validate=true — NEVER places an order)
    probe_pair = discovered[0] if discovered else "AAPLUSD"
    dryruns = {}
    for label, extra in (("plain", {}),
                         ("asset_class=equity", {"asset_class": "equity"}),
                         ("asset_class=tokenized_asset",
                          {"asset_class": "tokenized_asset"})):
        try:
            await call_private("/0/private/AddOrder", pub, priv, {
                "pair": probe_pair, "type": "buy", "ordertype": "limit",
                "price": "1.00", "volume": "1", "validate": "true", **extra,
            })
            dryruns[label] = {"status": "validated_ok"}
        except KrakenError as e:
            msg = "; ".join(e.errors) if hasattr(e, "errors") else str(e)
            low = msg.lower()
            if "permission" in low:
                status = "permission_denied"
            elif "unknown asset pair" in low or "unknown pair" in low:
                status = "pair_not_api_tradable"
            elif msg.startswith("EOrder") or "eorder" in low:
                status = "permission_granted_shape_rejected"
            else:
                status = "error"
            dryruns[label] = {"status": status, "kraken_says": msg[:200]}
        except Exception as e:  # noqa: BLE001
            dryruns[label] = {"status": "error", "detail": str(e)[:160]}
    caps["equity_order_dryrun"] = {"probe_pair": probe_pair, "attempts": dryruns}

    # verdict
    order_possible = any(
        d.get("status") in ("validated_ok", "permission_granted_shape_rejected")
        for d in dryruns.values())
    if discovered and order_possible:
        out["verdict"] = ("EQUITY_API_CAPABLE — instruments discoverable and "
                          "order validation passed; a staged migration is viable")
    elif discovered:
        out["verdict"] = ("INSTRUMENTS_ONLY — equity pairs visible but order "
                          "submission blocked; confirm key scope / account "
                          "eligibility with Kraken support")
    elif order_possible:
        out["verdict"] = ("ORDERS_MAYBE — validation passed but no equity "
                          "instruments discovered via public catalog; "
                          "confirm pair naming with Kraken support")
    else:
        out["verdict"] = ("NOT_API_CAPABLE — no equity instruments or order "
                          "path found on this key; keep Webull for equities")
    return out
