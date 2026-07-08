"""Polygon equity feeder — DAILY OHLCV via Polygon flatfiles (S3).

Doctrine pin (2026-02-19, operator directive):
    The REST grouped-daily poller (`shared/feeders/polygon_equity.py`)
    hit HTTP 403 NOT_AUTHORIZED on every attempt since 2026-06-11:

        "Attempted to request today's data before end of day.
         Please upgrade your plan at https://polygon.io/pricing"

    Cause: `_safe_to_pull` targets TODAY's date after 16:00 ET on
    a trading day. The operator's Polygon plan does NOT authorize
    "today's" grouped-daily data until Polygon marks the day
    settled — a distinct guarantee from "market has closed."

    Fix: switch to Polygon flatfiles (S3-backed, EOD-baked, T-1
    guaranteed on ALL paid tiers). Files are published ~4h after
    close (typically 04:00-04:30 UTC = 00:00-00:30 ET) and contain
    the SAME grouped-daily data as the REST endpoint.

    28 days of missing bars (2026-06-11 → 2026-07-07) will land in
    the first backfill run.

Why a NEW module instead of replacing the REST feeder:
    * Different auth (S3 sig-v4 vs REST bearer). Different failure
      modes. Isolating them keeps rollback trivial — set
      `POLYGON_FLATFILES_ENABLED=false` and MC returns to the REST
      poller instantly.
    * Same output schema — writes to `shared_ohlcv_bars` under the
      same `(source="polygon", tf="1d")` key, so consumers
      (`bar_source.load_recent_bars`, the resolver's price fetcher,
      snapshot enrichment) work unchanged.

Configuration (backend/.env):
    POLYGON_FLATFILES_ENABLED            "true" to enable (default true if creds set)
    POLYGON_FLATFILES_ENDPOINT           default: https://files.polygon.io
    POLYGON_FLATFILES_ACCESS_KEY         S3 access key ID
    POLYGON_FLATFILES_SECRET_KEY         S3 secret access key
    POLYGON_FLATFILES_BUCKET             default: flatfiles
    POLYGON_FLATFILES_POLL_INTERVAL_SEC  default: 3600 (1h) — files rarely
                                         land more than once per day
    POLYGON_FLATFILES_BACKFILL_DAYS      default: 45 (~one trading month
                                         plus buffer). On boot the worker
                                         walks backward this many days,
                                         pulling any missing files. Small
                                         because idempotent upserts are
                                         cheap; large enough to recover
                                         from a multi-week outage.

S3 endpoint:
    Polygon canonical is `https://files.polygon.io`. Operator's
    Massive mirror is `https://files.massive.com`. Both accept the
    same S3 creds and expose identical bucket layout — use whichever
    the operator's account is provisioned for via the env var.

Doctrine pin (data-integrity):
    Flatfiles are IMMUTABLE once published. The worker never
    upgrades a bar that already exists at the same
    (source, symbol, tf, ts) key. If Polygon ever republishes a
    file (rare, they've done it for corrections), the operator
    manually re-triggers via the admin endpoint — no auto-
    replacement, no silent data mutation.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from namespaces import SHARED_OHLCV_BARS
from shared.feeders.feeder_health import record_feeder_health
from shared.snapshots.nyse_calendar import is_trading_day


logger = logging.getLogger(__name__)


PROVIDER = "polygon_flatfiles"
FEEDER_SOURCE = "polygon"   # SAME source label as the REST feeder — consumers
                            # can't tell (and don't need to) which path fed them.
DEFAULT_TF = "1d"

DEFAULT_ENDPOINT = "https://files.polygon.io"
DEFAULT_BUCKET = "flatfiles"
DEFAULT_POLL_INTERVAL_SEC = 3600
DEFAULT_BACKFILL_DAYS = 45

# Path template inside the bucket. Polygon's schema (verified 2026-02-19).
KEY_TEMPLATE = "us_stocks_sip/day_aggs_v1/{yyyy}/{mm}/{yyyy}-{mm}-{dd}.csv.gz"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────── S3 client ───────────────────────────


_s3_client = None


def _get_s3_client():
    """Lazy boto3 client. boto3 is imported inside the function so
    unit tests that don't hit S3 don't need it installed."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    import boto3  # noqa: WPS433
    from botocore.config import Config  # noqa: WPS433

    endpoint = os.environ.get("POLYGON_FLATFILES_ENDPOINT", DEFAULT_ENDPOINT)
    ak = os.environ.get("POLYGON_FLATFILES_ACCESS_KEY", "")
    sk = os.environ.get("POLYGON_FLATFILES_SECRET_KEY", "")
    if not ak or not sk:
        raise RuntimeError(
            "POLYGON_FLATFILES_ACCESS_KEY / POLYGON_FLATFILES_SECRET_KEY missing"
        )
    _s3_client = boto3.Session(
        aws_access_key_id=ak, aws_secret_access_key=sk,
    ).client(
        "s3",
        endpoint_url=endpoint,
        config=Config(
            signature_version="s3v4",
            connect_timeout=15,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    return _s3_client


def _reset_s3_client() -> None:
    """Test hook: forget the cached client so env changes take effect."""
    global _s3_client
    _s3_client = None


# ─────────────────────────── Fetch + parse ───────────────────────────


def _key_for(bar_date: date) -> str:
    return KEY_TEMPLATE.format(
        yyyy=f"{bar_date.year:04d}",
        mm=f"{bar_date.month:02d}",
        dd=f"{bar_date.day:02d}",
    )


async def _fetch_day_bytes(bar_date: date) -> Optional[bytes]:
    """Download one day's flatfile. Returns None if the file doesn't
    exist yet (Polygon hasn't published it) — that's not an error,
    just "come back later."
    """
    bucket = os.environ.get("POLYGON_FLATFILES_BUCKET", DEFAULT_BUCKET)
    key = _key_for(bar_date)
    s3 = _get_s3_client()

    def _blocking_get():
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()
        except s3.exceptions.NoSuchKey:
            return None
        except Exception as e:
            # boto3 raises ClientError for 404 too in some paths.
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    # boto3 is sync; run in default executor so we don't block the loop.
    return await asyncio.get_running_loop().run_in_executor(None, _blocking_get)


def _parse_csv_gz(raw: bytes, bar_date: date) -> list[dict[str, Any]]:
    """Parse a Polygon day_aggs CSV.gz payload into MC bar rows.

    CSV schema (verified 2026-02-19):
        ticker, volume, open, close, high, low, window_start, transactions
    """
    text = gzip.decompress(raw).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    ts_iso = datetime.combine(
        bar_date, datetime.min.time(), tzinfo=timezone.utc,
    ).isoformat()
    out: list[dict[str, Any]] = []
    for row in reader:
        try:
            symbol = (row.get("ticker") or "").upper().strip()
            if not symbol or len(symbol) > 16:
                continue
            o = float(row["open"])
            c = float(row["close"])
            h = float(row["high"])
            l_ = float(row["low"])
            v = float(row.get("volume") or 0.0)
            trades_raw = row.get("transactions")
            trades = int(trades_raw) if trades_raw not in (None, "") else None
        except (ValueError, KeyError, TypeError):
            continue
        out.append({
            "source": FEEDER_SOURCE,
            "symbol": symbol,
            "tf": DEFAULT_TF,
            "ts": ts_iso,
            "o": o, "h": h, "l": l_, "c": c, "v": v,
            "trades": trades,
            "ingested_at": _now_iso(),
            "feeder": PROVIDER,
        })
    return out


async def _upsert_bars(bars: list[dict[str, Any]]) -> int:
    """Idempotent upsert on (source, symbol, tf, ts). Matches the REST
    feeder's write shape exactly so consumers can't tell them apart."""
    written = 0
    for bar in bars:
        try:
            await db[SHARED_OHLCV_BARS].update_one(
                {
                    "source": bar["source"],
                    "symbol": bar["symbol"],
                    "tf": bar["tf"],
                    "ts": bar["ts"],
                },
                {"$set": bar},
                upsert=True,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 — one bad row mustn't tank a batch
            await record_feeder_health(
                provider=PROVIDER, endpoint="_upsert_bars",
                status_code=None, error_type="db_error",
                message=f"{type(exc).__name__}: {exc}",
                context={"symbol": bar.get("symbol"), "ts": bar.get("ts")},
            )
    return written


async def pull_for_date(bar_date: date) -> dict[str, Any]:
    """Public entry point — pull and persist one day's flatfile.

    Idempotent: repeat calls for the same date upsert on
    (source, symbol, tf, ts). Never mutates already-present bars.
    """
    key = _key_for(bar_date)
    try:
        raw = await _fetch_day_bytes(bar_date)
    except Exception as exc:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint=key,
            status_code=None, error_type="s3_error",
            message=f"{type(exc).__name__}: {str(exc)[:400]}",
        )
        return {"ok": False, "date": bar_date.isoformat(), "error": str(exc)[:400]}

    if raw is None:
        await record_feeder_health(
            provider=PROVIDER, endpoint=key,
            status_code=404, error_type="not_yet_published",
            message="flatfile not published yet — try later",
        )
        return {"ok": False, "date": bar_date.isoformat(), "not_published": True}

    bars = _parse_csv_gz(raw, bar_date)
    written = await _upsert_bars(bars)
    await record_feeder_health(
        provider=PROVIDER, endpoint=key,
        status_code=200, error_type=None,
        message=f"pulled {len(bars)} bars, wrote {written}",
    )
    return {
        "ok": True,
        "date": bar_date.isoformat(),
        "bars_parsed": len(bars),
        "bars_written": written,
    }


# ─────────────────────────── Backfill + worker loop ───────────────────────────


async def _dates_needing_pull(backfill_days: int) -> list[date]:
    """Walk backward from yesterday, returning trading-day dates for
    which no bars are present in `shared_ohlcv_bars`. Skips non-
    trading days.

    Yesterday, not today: flatfiles are always T-1. Trying to pull
    "today" produces a 404 (file not yet baked) — noise, not signal.
    """
    today_utc = datetime.now(timezone.utc).date()
    missing: list[date] = []
    cursor = today_utc - timedelta(days=1)
    checked = 0
    while checked < backfill_days:
        if is_trading_day(cursor):
            ts_iso = datetime.combine(
                cursor, datetime.min.time(), tzinfo=timezone.utc,
            ).isoformat()
            present = await db[SHARED_OHLCV_BARS].count_documents({
                "source": FEEDER_SOURCE,
                "tf": DEFAULT_TF,
                "ts": ts_iso,
            })
            # 5000 rows = confidence the file was fully written; typical
            # day is ~12k rows. Same threshold the REST feeder uses.
            if present < 5000:
                missing.append(cursor)
        cursor -= timedelta(days=1)
        checked += 1
    # Return oldest-first so backfills fill sequentially.
    missing.reverse()
    return missing


async def _tick() -> dict[str, Any]:
    """One pass: determine what's missing, pull each. Bounded by
    POLYGON_FLATFILES_BACKFILL_DAYS on each call so a corrupted state
    can't produce unbounded S3 traffic."""
    backfill_days = _env_int(
        "POLYGON_FLATFILES_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS,
    )
    targets = await _dates_needing_pull(backfill_days)
    if not targets:
        return {"skipped": True, "reason": "no_missing_days"}
    results = []
    for d in targets:
        r = await pull_for_date(d)
        results.append(r)
    total_written = sum(int(r.get("bars_written") or 0) for r in results)
    return {
        "targets_processed": len(targets),
        "total_bars_written": total_written,
        "per_day": results,
    }


_stop_flag: bool = False
_task: Optional[asyncio.Task] = None


async def _worker_loop() -> None:
    global _stop_flag
    interval = _env_int(
        "POLYGON_FLATFILES_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC,
    )
    logger.info(
        "polygon_flatfiles worker started: interval=%ss backfill_days=%s",
        interval, _env_int("POLYGON_FLATFILES_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS),
    )
    while not _stop_flag:
        try:
            result = await _tick()
            if result.get("targets_processed", 0) > 0:
                logger.info("polygon_flatfiles tick: %s", {
                    k: v for k, v in result.items() if k != "per_day"
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("polygon_flatfiles tick error: %r", e)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop",
                status_code=None, error_type="worker_crash",
                message=str(e)[:500],
            )
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the polling task. Idempotent — re-callable on hot reload.
    No-op if disabled by env, or if S3 creds are missing (the health-
    audit row gets the diagnostic, not the caller)."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    enabled = _env_bool("POLYGON_FLATFILES_ENABLED", True)
    ak = os.environ.get("POLYGON_FLATFILES_ACCESS_KEY", "")
    sk = os.environ.get("POLYGON_FLATFILES_SECRET_KEY", "")
    if not enabled:
        logger.info(
            "polygon_flatfiles worker disabled via "
            "POLYGON_FLATFILES_ENABLED=false",
        )
        return
    if not (ak and sk):
        logger.info(
            "polygon_flatfiles worker not started — "
            "POLYGON_FLATFILES_ACCESS_KEY/SECRET_KEY missing",
        )
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="polygon_flatfiles")


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
