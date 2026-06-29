"""Server-time diagnostic endpoint.

Tiny no-deps endpoint that returns the server's wall clock + a probe of
real Internet time so the operator can `curl` it and immediately know
whether a Webull "INVALID_PARAMETER: The time you sent is not supported"
is genuinely a clock issue or a payload issue.

Doctrine pin (2026-02-26, operator directive — "Correct it by fixing the
server clock, not timezone."): the operator hits this endpoint first,
THEN we decide whether to patch the Webull SDK's signing timestamp.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import urllib.request

from fastapi import APIRouter, Depends

from auth import get_current_user


router = APIRouter(prefix="/admin", tags=["diagnostics"])


def _probe_internet_time() -> dict:
    """HEAD a public endpoint and parse its `Date` header — that's the
    real Internet wall clock per RFC 7231. Falls through a small list
    so a single CDN hiccup doesn't break the diagnostic."""
    probes = (
        os.environ.get("SERVER_TIME_PROBE_URLS")
        or "https://www.google.com,https://www.cloudflare.com,https://api.github.com"
    )
    for url in [u.strip() for u in probes.split(",") if u.strip()]:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=4) as resp:
                date_hdr = resp.headers.get("Date")
                if not date_hdr:
                    continue
                dt = parsedate_to_datetime(date_hdr).astimezone(timezone.utc)
                return {
                    "probe_url": url,
                    "date_header_raw": date_hdr,
                    "probed_utc": dt.isoformat(),
                    "probed_epoch": dt.timestamp(),
                }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    return {"probe_failed": True, "error": repr(last_err) if "last_err" in dir() else "no probes"}


@router.get("/server-time")
def server_time(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Returns server wall-clock + real-Internet-time probe + the skew
    between them. Use this to debug Webull "time you sent" errors.

    Read order:
      1. `system_utc`        — what the pod thinks the time is.
      2. `internet_probe`    — real wall-clock from Google/Cloudflare.
      3. `skew_seconds`      — `system_utc - internet_probe`. Should be
                                < ~30s. If it isn't, the broker's
                                signing verifier will reject every
                                request and that's the clock to fix.
    """
    sys_utc_dt = datetime.now(timezone.utc)
    probe = _probe_internet_time()

    skew_s = None
    if "probed_epoch" in probe:
        skew_s = sys_utc_dt.timestamp() - probe["probed_epoch"]

    # Webull-specific cross-check: report whether the clock-skew
    # compensator decided to patch the SDK or not, so the operator
    # has the full picture in one shot.
    webull_patch_state = None
    try:
        from shared.broker.webull_clock_skew import (  # noqa: WPS433
            current_offset_seconds, is_installed,
        )
        webull_patch_state = {
            "patch_installed": is_installed(),
            "offset_seconds_in_use": current_offset_seconds(),
        }
    except Exception:  # noqa: BLE001
        webull_patch_state = {"unavailable": True}

    return {
        "system_utc": sys_utc_dt.isoformat(),
        "system_epoch": sys_utc_dt.timestamp(),
        "internet_probe": probe,
        "skew_seconds": skew_s,
        "skew_is_within_30s": (skew_s is not None and abs(skew_s) <= 30),
        "webull_clock_patch": webull_patch_state,
        "ntp_env_note": (
            "This pod runs without systemd (no timedatectl). Clock is "
            "inherited from the kubelet/node. If skew_seconds > 30s, "
            "the fix is at the node/host level — not inside this app."
        ),
    }
