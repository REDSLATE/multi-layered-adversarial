"""FRED (Federal Reserve Economic Data) — macro series feeder.

Polls a fixed list of macro series daily and caches observations in
`alt_data_macro`. Brains may consume the macro state as a feature for
their decision models. MC stores; brains read; seat holder acts.

Series tracked (operator-tunable via FRED_SERIES_IDS env var,
comma-separated):
  CPIAUCNS   — CPI all items, not seasonally adjusted
  UNRATE     — civilian unemployment rate
  FEDFUNDS   — effective federal funds rate
  DGS10      — 10-year Treasury constant maturity rate
  T10Y2Y     — 10-year minus 2-year Treasury spread (recession signal)

Auth: ?api_key=<FRED_API_KEY>. Free signup at fred.stlouisfed.org.
Rate limit: 120 req/min — generous; we poll 5 series once a day.

Doctrine pin: descriptive evidence only. The `alt_data_macro`
collection MUST NOT carry `may_execute`. Tripwire-pinned.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from db import db
from namespaces import ALT_DATA_MACRO
from shared.feeders.feeder_health import record_feeder_health


logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org"
PROVIDER = "fred"

DEFAULT_SERIES = ("CPIAUCNS", "UNRATE", "FEDFUNDS", "DGS10", "T10Y2Y")


_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=FRED_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _client


async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def fetch_series(series_id: str, api_key: str) -> Optional[dict[str, Any]]:
    """GET /fred/series/observations?series_id=..."""
    try:
        resp = await _get_client().get(
            "/fred/series/observations",
            params={
                "series_id": series_id, "api_key": api_key,
                "file_type": "json",
            },
        )
    except httpx.RequestError as exc:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/fred/series/observations",
            status_code=None, error_type="request_error",
            message=str(exc), context={"series_id": series_id},
        )
        return None
    if resp.status_code == 429:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/fred/series/observations",
            status_code=429, error_type="rate_limit",
            message=f"retry-after={resp.headers.get('Retry-After')}",
            context={"series_id": series_id},
        )
        return None
    if resp.status_code >= 400:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/fred/series/observations",
            status_code=resp.status_code, error_type="http_status_error",
            message=resp.text[:500], context={"series_id": series_id},
        )
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint="/fred/series/observations",
            status_code=resp.status_code, error_type="api_error",
            message=str(exc)[:200], context={"series_id": series_id},
        )
        return None


def observations_to_docs(
    series_id: str, payload: dict[str, Any],
) -> list[dict[str, Any]]:
    units = payload.get("units")
    rt_start = payload.get("realtime_start")
    rt_end = payload.get("realtime_end")
    obs = payload.get("observations") or []
    docs: list[dict[str, Any]] = []
    for o in obs:
        if not isinstance(o, dict):
            continue
        v = o.get("value")
        if v in (None, "", "."):
            continue
        try:
            value = float(v)
        except (TypeError, ValueError):
            continue
        date = o.get("date")
        if not date:
            continue
        docs.append({
            "provider": PROVIDER,
            "series_id": series_id,
            "date": date,
            "value": value,
            "units": units,
            "realtime_start": rt_start,
            "realtime_end": rt_end,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        })
    return docs


async def _persist_obs(docs: list[dict[str, Any]]) -> int:
    inserted = 0
    for d in docs:
        # Tripwire-pinned: alt-data carries no execution authority.
        d.pop("may_execute", None)
        result = await db[ALT_DATA_MACRO].update_one(
            {"provider": d["provider"], "series_id": d["series_id"], "date": d["date"]},
            {"$set": d},
            upsert=True,
        )
        if result.upserted_id is not None:
            inserted += 1
    return inserted


_task: Optional[asyncio.Task] = None
_stop_flag = False


def _read_config() -> dict[str, Any]:
    series_env = os.environ.get("FRED_SERIES_IDS", "").strip()
    series = (
        tuple(s.strip().upper() for s in series_env.split(",") if s.strip())
        if series_env else DEFAULT_SERIES
    )
    return {
        "enabled": os.environ.get("FRED_ENABLED", "").lower() == "true",
        "api_key": os.environ.get("FRED_API_KEY", "").strip(),
        "interval": int(os.environ.get("FRED_POLL_INTERVAL_SEC", "86400")),
        "series": series,
    }


async def _poll_once(api_key: str, series: tuple[str, ...]) -> dict[str, Any]:
    if not series:
        return {"series": 0, "obs_inserted": 0}
    total = 0
    for sid in series:
        payload = await fetch_series(sid, api_key)
        if not payload:
            continue
        docs = observations_to_docs(sid, payload)
        if docs:
            total += await _persist_obs(docs)
    return {
        "series": len(series), "obs_inserted": total,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def _worker_loop() -> None:
    global _stop_flag
    while not _stop_flag:
        cfg = _read_config()
        if not cfg["enabled"]:
            await asyncio.sleep(60)
            continue
        if not cfg["api_key"]:
            await record_feeder_health(
                provider=PROVIDER, endpoint="(boot)", status_code=None,
                error_type="configuration", message="FRED_API_KEY missing",
            )
            await asyncio.sleep(3600)
            continue
        try:
            summary = await _poll_once(cfg["api_key"], cfg["series"])
            logger.info("fred poll: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fred poll crashed: %s", exc)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop", status_code=None,
                error_type="worker_crash", message=str(exc)[:500],
            )
        await asyncio.sleep(cfg["interval"])


def start_worker_if_enabled() -> None:
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    cfg = _read_config()
    if not cfg["enabled"]:
        logger.info("fred worker disabled (FRED_ENABLED!=true)")
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="fred_worker")
    logger.info("fred worker started (interval=%ss)", cfg["interval"])


async def stop_worker() -> None:
    global _task, _stop_flag
    _stop_flag = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
    await _close_client()
