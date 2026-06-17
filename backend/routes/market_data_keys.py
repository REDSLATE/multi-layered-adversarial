"""MC market-data key proxy — Doctrine-safe brain data-source bootstrap.

Operator decision (2026-05-28):
  Brain teams need their sidecars to fetch market data (bars, quotes,
  news, fundamentals) from third-party providers like Polygon,
  Finnhub, and Alpha Vantage. When the operator revoked broker keys
  from brain sidecars on 2026-05-23 to close the orphan-execution
  attack surface, the brains also lost their direct broker-API data
  pipe (they were using Alpaca/Kraken keys to READ market data, not
  just to trade). Without market data, brains can't compute
  features → score=0 → REJECT/HOLD on every snapshot.

Doctrine pin (D-DATA-KEYS-2026-05-28):
  MC may distribute DATA-SOURCE API keys (Polygon, Finnhub, Alpha
  Vantage, FRED, news APIs) to authenticated brain sidecars. MC must
  NEVER distribute BROKER API keys (Alpaca, Kraken, IBKR) under any
  circumstances. Broker keys remain solely in MC's process memory
  per the 2026-05-23 audit closure.

  The distinction is whether the key grants AUTHORITY TO TRADE:
    - DATA keys: read-only access to a data provider — fair game
    - BROKER keys: authority to place orders on a real account — never

  This endpoint enforces the boundary by WHITELIST. A field name must
  appear in `MARKET_DATA_KEY_FIELDS` to be served. The whitelist is
  tripwire-pinned in `tests/test_market_data_keys_proxy.py` to
  reject any future addition that contains broker-key field-name
  fragments (ALPACA, KRAKEN, IBKR, BROKER, EXECUTE).

  Auth: same `<BRAIN>_INGEST_TOKEN` pattern as `sidecar_checkin.py`.
  Brain sidecars send `X-Runtime-Token: <their token>` + `X-Brain-Id:
  <BRAIN>`. MC validates the pair against env. 401 on mismatch. Same
  identity surface the brain teams already wire for their existing
  /checkin path.

Endpoint:
  GET /api/admin/keys/market-data
      Headers: X-Runtime-Token, X-Brain-Id
      Response: {"keys": {field: value, ...}, "served_fields": [...],
                 "brain": <BRAIN>, "ts": ISO}

  Audit: every successful fetch is logged to `market_data_key_fetches`
  collection. Operator can revoke a brain's data access by rotating
  that brain's INGEST_TOKEN without touching the others.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.market_data_keys")

# ──────────────────────── WHITELIST ────────────────────────
# Tripwire-pinned: every field name MUST be a known data-source token.
# Adding a new field requires:
#   1. The name does NOT contain any of FORBIDDEN_FRAGMENTS
#   2. A corresponding env var exists in backend/.env
#   3. A tripwire is added documenting why the field is read-only
MARKET_DATA_KEY_FIELDS: tuple[str, ...] = (
    # Polygon — historical + WS market data
    "POLYGON_API_KEY",
    # Finnhub — equity OHLCV + news + filings (also used by our feeder)
    "FINNHUB_API_KEY",
    # Alpha Vantage — historical bars, fundamentals
    "ALPHA_VANTAGE_API_KEY",
    # FRED — macro series
    "FRED_API_KEY",
    # NewsAPI — headline ingest (read-only)
    "NEWSAPI_API_KEY",
    # SEC EDGAR — no key, but the User-Agent string is required
    "SEC_EDGAR_USER_AGENT",
)

# Forbidden substrings — any field name containing these is REJECTED
# regardless of whether someone adds it to MARKET_DATA_KEY_FIELDS by
# mistake. Defence in depth against future authoring errors.
FORBIDDEN_FRAGMENTS: tuple[str, ...] = (
    "ALPACA",   # broker (equity)
    "KRAKEN",   # broker (crypto)
    "IBKR",     # broker (Interactive Brokers)
    "COINBASE", # broker (crypto)
    "BINANCE",  # broker (crypto)
    "BROKER",
    "SECRET_KEY",        # Alpaca uses this naming
    "EXECUTE",
    "TRADING_TOKEN",
    "BROKER_TOKEN",
)


def _validate_field_safe(field: str) -> bool:
    """A field name is safe to serve iff:
      1. It's in the whitelist
      2. None of FORBIDDEN_FRAGMENTS appear in it
    Both conditions checked; defense in depth.
    """
    if field not in MARKET_DATA_KEY_FIELDS:
        return False
    upper = field.upper()
    for frag in FORBIDDEN_FRAGMENTS:
        if frag in upper:
            return False
    return True


# ──────────────────────── brain identity ────────────────────────
# Mirrors sidecar_checkin.py's pattern. Same INGEST_TOKEN per brain.

KNOWN_BRAINS: tuple[str, ...] = ("camino", "barracuda", "hellcat", "gto")


def _expected_token_for(brain: str) -> str:
    """Returns the env-configured ingest token for `brain`."""
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "")


def _authenticate(
    brain_header: Optional[str], runtime_token: Optional[str],
) -> str:
    """Validate the (X-Brain-Id, X-Runtime-Token) pair. Returns
    canonical brain name on success; raises HTTPException on fail.
    """
    brain = (brain_header or "").lower().strip()
    if not brain:
        raise HTTPException(
            status_code=401, detail="X-Brain-Id header required",
        )
    if brain not in KNOWN_BRAINS:
        raise HTTPException(
            status_code=404, detail=f"unknown brain {brain!r}",
        )
    expected = _expected_token_for(brain)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                f"market-data key proxy not configured for {brain}: "
                f"set {brain.upper()}_INGEST_TOKEN in backend/.env"
            ),
        )
    if (runtime_token or "") != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return brain


# ──────────────────────── routes ────────────────────────

router = APIRouter(
    prefix="/admin/keys",
    tags=["market-data-keys"],
)


@router.get("/market-data")
async def get_market_data_keys(
    x_brain_id: Optional[str] = Header(None, alias="X-Brain-Id"),
    x_runtime_token: Optional[str] = Header(None, alias="X-Runtime-Token"),
) -> dict:
    """Serve MARKET-DATA keys (read-only data-source tokens) to an
    authenticated brain sidecar.

    Doctrine: This endpoint MUST NEVER return broker keys. Field-name
    whitelist + forbidden-fragment check enforces this by construction.
    Tripwire-pinned.

    Audit: each successful fetch writes one row to
    `market_data_key_fetches` for forensic trail. Operator can revoke
    a brain by rotating its INGEST_TOKEN.
    """
    brain = _authenticate(x_brain_id, x_runtime_token)

    keys: dict[str, str] = {}
    served_fields: list[str] = []
    unconfigured_fields: list[str] = []

    for field in MARKET_DATA_KEY_FIELDS:
        # Defence in depth: every field passes the boundary check
        # before being read from env. If someone slips a broker key
        # name into the whitelist, this still rejects it.
        if not _validate_field_safe(field):
            logger.error(
                "market_data_keys: WHITELIST POISONED — field %s rejected "
                "by forbidden-fragment check. Doctrine violation prevented.",
                field,
            )
            continue
        val = (os.environ.get(field) or "").strip()
        if val:
            keys[field] = val
            served_fields.append(field)
        else:
            unconfigured_fields.append(field)

    # Audit log — best-effort, never blocks the response.
    try:
        await db["market_data_key_fetches"].insert_one({
            "brain": brain,
            "served_fields": served_fields,
            "unconfigured_fields": unconfigured_fields,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("market_data_keys audit log failed: %s", exc)

    return {
        "brain": brain,
        "keys": keys,
        "served_fields": served_fields,
        "unconfigured_fields": unconfigured_fields,
        "doctrine": "market_data_only",
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/market-data/manifest")
async def get_market_data_manifest() -> dict:
    """Public-ish manifest of what fields MC will serve to brains.
    Returns field NAMES only — never values. Used by brain teams to
    know which env vars they should expect to receive without making
    an authenticated request.

    Open auth (no X-Brain-Id required) because the field list is
    static; revealing it doesn't help an attacker.
    """
    # Tripwire: assert no forbidden fragment slipped into whitelist.
    safe_fields = [f for f in MARKET_DATA_KEY_FIELDS if _validate_field_safe(f)]
    return {
        "fields": safe_fields,
        "forbidden_fragments": list(FORBIDDEN_FRAGMENTS),
        "doctrine": (
            "MC distributes data-source keys to authenticated brain "
            "sidecars. MC NEVER distributes broker keys."
        ),
        "auth": "X-Brain-Id + X-Runtime-Token (same <BRAIN>_INGEST_TOKEN as sidecar_checkin)",
        "audit_collection": "market_data_key_fetches",
    }


@router.get("/market-data/audit")
async def get_market_data_audit(
    limit: int = 50,
    brain: Optional[str] = None,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Operator-only audit of who has fetched market-data keys.

    Doctrine: read-only visibility into the `market_data_key_fetches`
    collection. The proxy itself records the audit row on every
    successful GET; this endpoint exposes it for operator dashboards
    so we can answer "which brains have actually wired the proxy?"
    in one curl, without each brain team duplicating audit logic
    on their side.

    Default `limit=50`, max 500. Optional `brain` filter for a
    per-brain view ("when did Chevelle first fetch?").
    """
    limit = max(1, min(500, int(limit)))
    query: dict = {}
    if brain:
        query["brain"] = brain.strip().lower()

    rows = await db["market_data_key_fetches"].find(
        query, {"_id": 0},
    ).sort("ts", -1).limit(limit).to_list(length=limit)

    # Per-brain rollup over the returned slice. Cheap, useful at a glance.
    by_brain: dict[str, dict] = {}
    for r in rows:
        b = r.get("brain") or "?"
        d = by_brain.setdefault(b, {"count": 0, "last_ts": None})
        d["count"] += 1
        ts = r.get("ts")
        if ts and (d["last_ts"] is None or ts > d["last_ts"]):
            d["last_ts"] = ts

    return {
        "items": rows,
        "count": len(rows),
        "by_brain": by_brain,
        "doctrine": "operator_read_only",
    }
