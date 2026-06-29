"""Webull SDK clock-skew compensator.

Why this exists
---------------
The Webull OpenAPI signs every request with an ISO-8601 timestamp pulled
from `datetime.utcnow()` (see `webull/core/utils/common.py::get_iso_8601_date`).
Webull's signing verifier rejects requests whose timestamp drifts more
than a few seconds from the server's wall clock with:

    HTTP 417 / INVALID_PARAMETER: "The time you sent is not supported. Please check."

The RISEDUAL Mission Control container runs on a Kubernetes pod whose
system clock can be set to a fictional date (e.g. 2026-06-29 in the
preview pod). We cannot change the OS clock, but we can intercept the
two `common.py` helpers the SDK uses to stamp every outbound request,
and replace `datetime.utcnow()` with `datetime.utcnow() + offset`, where
`offset = real_wall_clock - system_clock`.

How the offset is measured
--------------------------
On first patch, we HEAD `https://www.google.com` and read the `Date`
response header — an RFC 7231 timestamp the server stamps in real
wall-clock time. We parse it to a UTC datetime and subtract our system
`datetime.utcnow()` to compute the skew. If the skew is below
`WEBULL_CLOCK_SKEW_TOLERANCE_SECONDS` (default 30s), we leave the SDK
alone. If the skew exceeds the tolerance, we install the patch and log
loudly.

The offset is refreshed every `WEBULL_CLOCK_SYNC_INTERVAL_SECONDS`
(default 6h) by storing the timestamp of the last sync and re-fetching
on the next SDK call after that interval.

Safety
------
* Patch is idempotent — calling `install_webull_clock_skew_compensator`
  twice is a no-op.
* Falls open: if the network probe fails, we DO NOT install the patch
  (better to surface the real 417 than to ship a degenerate timestamp).
* The patch ONLY touches the two `webull.core.utils.common` helpers
  documented above. No other module is monkey-patched.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional


logger = logging.getLogger("risedual.webull.clock_skew")


_INSTALLED = False
_LOCK = threading.Lock()

# Skew between Webull's real wall clock and our system clock, expressed
# as a `timedelta` to be ADDED to `datetime.utcnow()` before signing.
_OFFSET: timedelta = timedelta(0)
_LAST_SYNC_MONOTONIC: float = 0.0


def _tolerance_seconds() -> float:
    try:
        return float(os.environ.get("WEBULL_CLOCK_SKEW_TOLERANCE_SECONDS", "30"))
    except (TypeError, ValueError):
        return 30.0


def _sync_interval_seconds() -> float:
    try:
        return float(os.environ.get("WEBULL_CLOCK_SYNC_INTERVAL_SECONDS", "21600"))
    except (TypeError, ValueError):
        return 21600.0  # 6h


def _probe_real_utc() -> Optional[datetime]:
    """HEAD a couple of public endpoints and parse their `Date` header.
    Falls through the list until one succeeds. Returns `None` if every
    probe fails (caller treats this as "skip the patch")."""
    probes = (
        os.environ.get("WEBULL_CLOCK_PROBE_URL")
        or "https://www.google.com,https://www.cloudflare.com,https://api.github.com"
    )
    for url in [u.strip() for u in probes.split(",") if u.strip()]:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=4) as resp:
                date_hdr = resp.headers.get("Date")
                if not date_hdr:
                    continue
                dt = parsedate_to_datetime(date_hdr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("clock probe %s failed: %s", url, exc)
            continue
    return None


def _measure_offset() -> Optional[timedelta]:
    """Returns (real_utc − system_utc), or None if the probe failed."""
    real_utc = _probe_real_utc()
    if real_utc is None:
        return None
    sys_utc = datetime.utcnow()
    offset = real_utc - sys_utc
    return offset


def _refresh_offset_if_due() -> None:
    """Re-fetch the wall-clock offset on the configured cadence.
    Cheap fast-path when not due (single int compare)."""
    global _OFFSET, _LAST_SYNC_MONOTONIC
    if (time.monotonic() - _LAST_SYNC_MONOTONIC) < _sync_interval_seconds():
        return
    new_off = _measure_offset()
    if new_off is None:
        return
    _OFFSET = new_off
    _LAST_SYNC_MONOTONIC = time.monotonic()
    logger.info(
        "webull clock-skew refresh: real_utc - sys_utc = %+.1fs",
        new_off.total_seconds(),
    )


def install_webull_clock_skew_compensator() -> dict:
    """Idempotent installer.

    Returns a small dict describing what we did so the adapter can
    log the decision.
    """
    global _INSTALLED, _OFFSET, _LAST_SYNC_MONOTONIC

    with _LOCK:
        if _INSTALLED:
            return {"installed": True, "skipped": "already_installed",
                    "offset_seconds": _OFFSET.total_seconds()}

        # Allow operator to FORCE-install the patch even if the
        # initial probe matches system clock — useful for testing.
        force = (os.environ.get("WEBULL_CLOCK_PATCH_FORCE", "")
                 .strip().lower() in {"1", "true", "yes", "on"})

        offset = _measure_offset()
        if offset is None:
            logger.warning(
                "webull clock-skew probe failed (network); SDK timestamp "
                "left untouched. Set WEBULL_CLOCK_PROBE_URL to override.",
            )
            return {"installed": False, "skipped": "probe_failed"}

        skew_s = offset.total_seconds()
        tolerance = _tolerance_seconds()

        if (not force) and abs(skew_s) <= tolerance:
            logger.info(
                "webull clock-skew %+.1fs is within tolerance (%.1fs); "
                "SDK timestamp left untouched.",
                skew_s, tolerance,
            )
            return {"installed": False, "skipped": "within_tolerance",
                    "offset_seconds": skew_s, "tolerance_seconds": tolerance}

        _OFFSET = offset
        _LAST_SYNC_MONOTONIC = time.monotonic()

        # ─── Install patch ───────────────────────────────────────────
        try:
            from webull.core.utils import common as wb_common  # noqa: WPS433
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "webull SDK not importable, clock-skew patch skipped: %s",
                exc,
            )
            return {"installed": False, "skipped": "sdk_missing"}

        orig_iso = wb_common.get_iso_8601_date
        orig_iso_ms = wb_common.get_iso_8601_date_with_millis

        FMT = "%Y-%m-%dT%H:%M:%SZ"
        FMT_MS = "%Y-%m-%dT%H:%M:%S.%fZ"

        def _patched_iso(dt_as_utc=None):
            if dt_as_utc is not None:
                return orig_iso(dt_as_utc)
            _refresh_offset_if_due()
            return (datetime.utcnow() + _OFFSET).strftime(FMT)

        def _patched_iso_ms(dt_as_utc=None):
            if dt_as_utc is not None:
                return orig_iso_ms(dt_as_utc)
            _refresh_offset_if_due()
            ret = (datetime.utcnow() + _OFFSET).strftime(FMT_MS)
            # mirror SDK's 23-char ISO-8601-with-ms layout
            if len(ret) != 27:
                # Defensive: format width should be deterministic, but
                # if it isn't we fall back to the SDK's own formatter
                # so we never ship a malformed Date header.
                return orig_iso_ms()
            return ret[:-4] + ret[-1:]

        wb_common.get_iso_8601_date = _patched_iso
        wb_common.get_iso_8601_date_with_millis = _patched_iso_ms

        _INSTALLED = True
        logger.warning(
            "webull clock-skew compensator INSTALLED: real_utc - sys_utc "
            "= %+.1fs (tolerance %.1fs). All SDK signing timestamps will "
            "be shifted forward by this offset.",
            skew_s, tolerance,
        )
        return {
            "installed": True,
            "offset_seconds": skew_s,
            "tolerance_seconds": tolerance,
            "real_utc_sample": (datetime.utcnow() + _OFFSET).isoformat() + "Z",
            "sys_utc_sample": datetime.utcnow().isoformat() + "Z",
        }


def current_offset_seconds() -> float:
    """Diagnostic accessor — exposed via the broker-health admin tile
    if we want it later."""
    return _OFFSET.total_seconds()


def is_installed() -> bool:
    return _INSTALLED
