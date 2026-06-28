"""Polygon/Massive news+sentiment witness ingestor.

Doctrine frame (TRIAL COURT, NOT A VOTING SYSTEM):
    Pulls news articles from the Polygon/Massive `/v2/reference/news`
    endpoint and lands them in the `external_signals` holding cell.
    Every signal lands DEFAULT-HOSTILE:
        verifier_status   = "UNTRUSTED"
        influence_allowed = False
    The Governor returns a 0.0 modifier for any signal where
    `influence_allowed=False`. The Seat sees these as dimmed,
    read-only context. Nothing this module writes can affect a trade.

    Verifier (future) judges whether `polygon` as a source ever
    earns weight via the four-phase progression. Until then, the
    witness council is silent in execution.

What gets written:
    For each article returned by /v2/reference/news, we iterate the
    `insights[]` array (one entry per ticker the article mentions).
    Each `(article, ticker)` tuple produces ONE `ExternalSignal`:
        side                       = positive→BUY, neutral→HOLD,
                                     negative→SELL
        self_reported_confidence   = Massive's categorical sentiment
                                     mapped to a fixed display float
                                     (ADVISORY ONLY — not load-bearing)
        reason                     = the full `sentiment_reasoning`
                                     (truncated to 500 chars)
        bar_close_ts               = `published_utc`
        dedup_key                  = "polygon:<TICKER>:-:news:<article_id>"
        raw                        = verbatim Polygon payload row

    The unique index on `dedup_key` makes the upsert idempotent: re-
    running the pull no-ops on already-seen (article, ticker) pairs.

    On first sight of `source="polygon"` we $setOnInsert a fresh
    UNTRUSTED row in `external_source_credibility`. Subsequent
    inserts never mutate existing fields — only Verifier may.

Configuration:
    POLYGON_API_KEY                       existing env var (Massive uses same key)
    POLYGON_BASE_URL                      defaults to https://api.massive.com
    POLYGON_NEWS_WITNESS_ENABLED          "true" to enable (default true if key set)
    POLYGON_NEWS_WITNESS_INTERVAL_SEC     default 3600 (1h)
    POLYGON_NEWS_WITNESS_LIMIT            articles-per-tick (default 100)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from pymongo.errors import DuplicateKeyError

from db import db
from namespaces import EXTERNAL_SIGNALS, EXTERNAL_SOURCE_CREDIBILITY
from shared.external_signals.models import (
    ExternalSignal,
    ExternalSourceCredibility,
)
from shared.feeders.feeder_health import record_feeder_health


logger = logging.getLogger(__name__)


WITNESS_SOURCE = "polygon"
PROVIDER = "polygon_news_witness"
DEFAULT_BASE_URL = "https://api.massive.com"
REASONING_MAX_CHARS = 500
DEFAULT_INTERVAL_SEC = 3600  # 1h — news doesn't move bar-by-bar
DEFAULT_LIMIT_PER_TICK = 100


# Massive's `sentiment` is categorical. We map to a fixed float
# purely for the display column. The Seat does NOT read this number
# (witness influence is gated by `influence_allowed`, which is
# False by default). Keeping a stable map here so the rendered
# diagnostic column doesn't oscillate.
SENTIMENT_TO_SIDE: dict[str, str] = {
    "positive": "BUY",
    "negative": "SELL",
    "neutral": "HOLD",
}
SENTIMENT_TO_DISPLAY_CONFIDENCE: dict[str, float] = {
    "positive": 0.65,
    "negative": 0.65,
    "neutral":  0.50,
}


# ──────────────────────── HTTP client (lazy) ────────────────────────


_client: Optional[httpx.AsyncClient] = None


def _get_client(base_url: str) -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ──────────────────────── fetch ────────────────────────


async def fetch_news(
    api_key: str,
    *,
    limit: int = 50,
    published_utc_gte: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
) -> Optional[list[dict[str, Any]]]:
    """Pull recent news articles from /v2/reference/news.

    Returns the `results[]` list on success, None on error (one
    `feeder_health_audit` row written on failure). When
    `published_utc_gte` is supplied, only articles at/after that
    ISO timestamp are returned — used as a high-water mark by the
    worker so we don't re-process the same articles every cycle.
    """
    params: dict[str, Any] = {
        "order": "desc",
        "sort": "published_utc",
        "limit": max(1, min(limit, 1000)),
        "apiKey": api_key,
    }
    if published_utc_gte:
        params["published_utc.gte"] = published_utc_gte

    path = "/v2/reference/news"
    try:
        resp = await _get_client(base_url).get(path, params=params)
        if resp.status_code != 200:
            await record_feeder_health(
                provider=PROVIDER, endpoint=path,
                status_code=resp.status_code,
                error_type="http_status_error",
                message=resp.text[:500],
                context={"limit": limit, "published_utc_gte": published_utc_gte},
            )
            return None
        data = resp.json()
        if data.get("status") != "OK":
            await record_feeder_health(
                provider=PROVIDER, endpoint=path,
                status_code=resp.status_code,
                error_type="api_error",
                message=str(data)[:500],
                context={"status": data.get("status")},
            )
            return None
        return data.get("results") or []
    except Exception as exc:  # noqa: BLE001 — bounded network call
        await record_feeder_health(
            provider=PROVIDER, endpoint=path,
            status_code=None,
            error_type="request_error",
            message=f"{type(exc).__name__}: {exc}",
        )
        return None


# ──────────────────────── transform ────────────────────────


def article_to_witness_rows(
    article: dict[str, Any],
    *,
    symbol_universe: Optional[set[str]] = None,
) -> list[ExternalSignal]:
    """Transform one Polygon news article into N witness rows
    (one per insight whose ticker is in the universe).

    If `symbol_universe` is None, emit a row for every insight.
    If supplied (the operator's watchlist), emit only for insights
    on watched tickers — keeps the holding cell focused.

    Doctrine: every emitted row is default-hostile. We do not set
    `verifier_status` or `influence_allowed` here — the model
    defaults handle it. Anyone changing those defaults breaks the
    test_external_signal_defaults_hostile guard.
    """
    article_id = article.get("id")
    published_utc = article.get("published_utc")
    if not article_id or not published_utc:
        return []

    insights = article.get("insights") or []
    if not isinstance(insights, list):
        return []

    rows: list[ExternalSignal] = []
    for insight in insights:
        if not isinstance(insight, dict):
            continue
        ticker = (insight.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        if symbol_universe is not None and ticker not in symbol_universe:
            continue
        sentiment = (insight.get("sentiment") or "").strip().lower()
        side = SENTIMENT_TO_SIDE.get(sentiment)
        if side is None:
            # Unknown sentiment value — skip rather than guess.
            # Verifier can't grade something we couldn't classify.
            continue
        confidence = SENTIMENT_TO_DISPLAY_CONFIDENCE.get(sentiment, 0.50)
        reasoning = (insight.get("sentiment_reasoning") or "")[:REASONING_MAX_CHARS]
        # bar_close_ts (per doctrine) stays as `published_utc` — that's
        # the canonical time the witness fact was emitted, and what
        # Verifier reads when correlating to later outcomes. For
        # dedup, however, the `article_id` is the stronger uniqueness
        # key: two articles published at the same minute that both
        # mention the same ticker are STILL two distinct witness
        # facts. Using published_utc here would silently swallow the
        # second one — an idempotency leak.
        dedup_key = f"{WITNESS_SOURCE}:{ticker}:news:{article_id}"
        rows.append(ExternalSignal(
            source=WITNESS_SOURCE,
            symbol=ticker,
            side=side,
            self_reported_confidence=confidence,
            timeframe=None,
            event="news",
            reason=reasoning or None,
            raw=article,
            bar_close_ts=published_utc,
            dedup_key=dedup_key,
            # verifier_status / influence_allowed left at model defaults
            # (UNTRUSTED / False) — doctrine pin.
        ))
    return rows


# ──────────────────────── persist ────────────────────────


async def upsert_credibility_setoninsert() -> None:
    """Idempotently land a fresh UNTRUSTED case file for the polygon
    source. `$setOnInsert` semantics: if the row already exists,
    NOTHING is mutated — protects against a hostile witness silently
    re-setting itself to UNTRUSTED after Verifier promotes it.
    """
    row = ExternalSourceCredibility(source=WITNESS_SOURCE).model_dump()
    await db[EXTERNAL_SOURCE_CREDIBILITY].update_one(
        {"source": WITNESS_SOURCE},
        {"$setOnInsert": row},
        upsert=True,
    )


async def write_witness_rows(rows: list[ExternalSignal]) -> dict[str, int]:
    """Insert witness rows. Idempotent on `dedup_key` via the unique
    index. Returns counts so the operator can read what happened.
    """
    inserted = 0
    duplicates = 0
    errors = 0
    for row in rows:
        doc = row.model_dump()
        try:
            await db[EXTERNAL_SIGNALS].insert_one(doc)
            inserted += 1
        except DuplicateKeyError:
            # Same article × same ticker already landed — fine, that's
            # the idempotency contract. No mutation, no error.
            duplicates += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(
                "polygon_news_witness: failed to insert row dedup_key=%s err=%s",
                row.dedup_key, exc,
            )
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors}


# ──────────────────────── one-shot tick ────────────────────────


async def tick(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    limit: int = 50,
    published_utc_gte: Optional[str] = None,
    symbol_universe: Optional[set[str]] = None,
) -> dict[str, Any]:
    """One end-to-end pass: fetch → transform → upsert credibility
    → write rows. Public for testability and for an admin-triggered
    one-shot endpoint later.

    Returns a summary dict. NEVER raises — failures land in
    `feeder_health_audit` and the summary's `ok` field flips False.
    """
    key = api_key or (os.environ.get("POLYGON_API_KEY") or "").strip()
    if not key:
        return {
            "ok": False, "reason": "POLYGON_API_KEY missing",
            "articles_fetched": 0, "rows_built": 0,
        }

    articles = await fetch_news(
        key,
        limit=limit,
        published_utc_gte=published_utc_gte,
        base_url=base_url or DEFAULT_BASE_URL,
    )
    if articles is None:
        return {
            "ok": False, "reason": "fetch_failed_see_feeder_health_audit",
            "articles_fetched": 0, "rows_built": 0,
        }

    # Land the case file row idempotently before we write any
    # witness rows. This way the credibility ledger is in place
    # before the first witness lands, even on a fresh database.
    await upsert_credibility_setoninsert()

    rows: list[ExternalSignal] = []
    for article in articles:
        rows.extend(article_to_witness_rows(article, symbol_universe=symbol_universe))

    write_summary = await write_witness_rows(rows)
    summary = {
        "ok": True,
        "articles_fetched": len(articles),
        "rows_built": len(rows),
        **write_summary,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("polygon_news_witness tick: %s", summary)
    return summary


__all__ = (
    "WITNESS_SOURCE",
    "PROVIDER",
    "DEFAULT_BASE_URL",
    "SENTIMENT_TO_SIDE",
    "SENTIMENT_TO_DISPLAY_CONFIDENCE",
    "fetch_news",
    "article_to_witness_rows",
    "upsert_credibility_setoninsert",
    "write_witness_rows",
    "tick",
    "start_worker_if_enabled",
    "stop_worker",
    "close_client",
)


# ──────────────────────── worker loop ────────────────────────


_task: Optional[asyncio.Task] = None
_stop_flag: bool = False


def _read_worker_config() -> dict[str, Any]:
    api_key = (os.environ.get("POLYGON_API_KEY") or "").strip()
    enabled_env = (os.environ.get("POLYGON_NEWS_WITNESS_ENABLED") or "").strip().lower()
    # Default-on when key is present; explicit "false" disables.
    if enabled_env == "false":
        enabled = False
    elif enabled_env == "true":
        enabled = True
    else:
        enabled = bool(api_key)
    return {
        "api_key": api_key,
        "enabled": enabled,
        "interval": int(
            os.environ.get("POLYGON_NEWS_WITNESS_INTERVAL_SEC", str(DEFAULT_INTERVAL_SEC))
        ),
        "limit": int(
            os.environ.get("POLYGON_NEWS_WITNESS_LIMIT", str(DEFAULT_LIMIT_PER_TICK))
        ),
        "base_url": os.environ.get("POLYGON_BASE_URL", DEFAULT_BASE_URL),
    }


async def _high_water_mark() -> Optional[str]:
    """Find the most recent `bar_close_ts` we've already persisted for
    source=polygon. Used as `published_utc.gte` on the next fetch so
    we don't re-stream the entire article history every tick.

    Returns None when the holding cell has no polygon rows yet — the
    first tick pulls a fresh window without a floor.
    """
    doc = await db[EXTERNAL_SIGNALS].find_one(
        {"source": WITNESS_SOURCE},
        sort=[("bar_close_ts", -1)],
        projection={"bar_close_ts": 1, "_id": 0},
    )
    return doc.get("bar_close_ts") if doc else None


async def _worker_loop() -> None:
    """Periodic poll → fetch → transform → persist. Uses the
    high-water mark from the holding cell to avoid re-streaming. The
    unique `dedup_key` index makes the loop safe even if the mark
    is wrong — duplicates no-op.
    """
    global _stop_flag
    cfg = _read_worker_config()
    logger.info(
        "polygon_news_witness worker started: interval=%ss limit=%s base_url=%s",
        cfg["interval"], cfg["limit"], cfg["base_url"],
    )
    while not _stop_flag:
        try:
            cfg = _read_worker_config()
            if not cfg["enabled"] or not cfg["api_key"]:
                await record_feeder_health(
                    provider=PROVIDER, endpoint="(boot)",
                    status_code=None, error_type="configuration",
                    message=(
                        "POLYGON_API_KEY missing or "
                        "POLYGON_NEWS_WITNESS_ENABLED=false"
                    ),
                )
                await asyncio.sleep(max(cfg["interval"], 300))
                continue
            hwm = await _high_water_mark()
            summary = await tick(
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
                limit=cfg["limit"],
                published_utc_gte=hwm,
            )
            logger.info("polygon_news_witness tick: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("polygon_news_witness loop crashed: %s", exc)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop",
                status_code=None, error_type="worker_crash",
                message=str(exc)[:500],
            )
        try:
            await asyncio.sleep(cfg["interval"])
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the polling task. Idempotent — re-callable on hot reload."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    cfg = _read_worker_config()
    if not cfg["enabled"]:
        logger.info(
            "polygon_news_witness worker disabled "
            "(POLYGON_NEWS_WITNESS_ENABLED=false or POLYGON_API_KEY missing)"
        )
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="polygon_news_witness")
    logger.info(
        "polygon_news_witness worker scheduled (interval=%ss)", cfg["interval"],
    )


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
    await close_client()
