#!/usr/bin/env python3
"""Universe refresh watchdog — a print-deltas poller for the first
prod hour after deploy.

Usage (from anywhere with backend/.env accessible):

    cd /app/backend && python3 scripts/watch_universe_refresh.py

    # Or with a custom cadence:
    cd /app/backend && python3 scripts/watch_universe_refresh.py --interval 60

What it prints:
  * Every N seconds (default 30), the latest refresh report per lane
  * If `provider_error != null` on the newest row, prints the FULL
    exception summary in red and rings the terminal bell
  * If `used_last_good=True` on the newest row (any reason), prints
    it in yellow
  * If `published=True` and `provider_error=null`, one-line green OK

Doctrine (2026-07-15 iter-30 P4b):
    Passive audit trail with no reader is the same class of bug as
    silent-partial-truth. This script exists so the first prod hour
    after a P4/P4b deploy has an actual pair of human eyeballs on
    the provider_error signal — not just a Mongo document nobody's
    querying.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Bootstrap paths so `python3 scripts/watch_universe_refresh.py` works
# from /app/backend without needing to install the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import db  # noqa: E402
from namespaces import UNIVERSE_REFRESH_REPORTS  # noqa: E402


RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
BELL = "\a"


async def _latest_by_lane() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for lane in ("equity", "crypto"):
        cur = db[UNIVERSE_REFRESH_REPORTS].find(
            {"lane": lane}, {"_id": 0},
        ).sort("refreshed_at", -1).limit(1)
        docs = await cur.to_list(1)
        if docs:
            out[lane] = docs[0]
    return out


def _fmt(report: dict) -> str:
    lane = report.get("lane", "?")
    at = (report.get("refreshed_at") or "")[:19]
    published = report.get("published", False)
    ulg = report.get("used_last_good", False)
    provider_error = report.get("provider_error")
    publish_error = report.get("publish_error")
    sizes = report.get("sizes") or {}
    final = sizes.get("final", 0)
    added = len(report.get("added") or [])
    removed = len(report.get("removed") or [])

    if provider_error:
        head = f"{RED}{BOLD}[PROVIDER FAIL]{RESET}"
        tail = f"{RED}{provider_error}{RESET}"
        bell = BELL
    elif ulg:
        head = f"{YELLOW}{BOLD}[USED LAST-GOOD]{RESET}"
        tail = f"{YELLOW}{publish_error or 'no-error'}{RESET}"
        bell = ""
    elif published:
        head = f"{GREEN}[OK]{RESET}"
        tail = f"final={final} added={added} removed={removed}"
        bell = ""
    else:
        head = f"{RED}{BOLD}[UNKNOWN]{RESET}"
        tail = repr(report)[:120]
        bell = BELL

    return f"{bell}{head} {DIM}{at}{RESET} lane={lane:6s} {tail}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30,
                    help="Poll interval in seconds (default 30)")
    args = ap.parse_args()

    print(
        f"{BOLD}universe refresh watchdog{RESET} "
        f"(interval={args.interval}s, "
        f"reading `{UNIVERSE_REFRESH_REPORTS}`)",
    )
    print(f"{DIM}green=OK, yellow=used-last-good, red=provider-error{RESET}")
    print()

    seen: dict[str, str] = {}  # lane -> last-seen refreshed_at
    while True:
        try:
            latest = await _latest_by_lane()
        except Exception as exc:  # noqa: BLE001
            print(f"{RED}poll failed: {exc}{RESET}", flush=True)
            latest = {}
        for lane, report in latest.items():
            at = report.get("refreshed_at") or ""
            if at != seen.get(lane):
                seen[lane] = at
                print(_fmt(report), flush=True)
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        pass
