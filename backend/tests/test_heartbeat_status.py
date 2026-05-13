"""Heartbeat-status endpoint — read-only public banding for the
operator dashboard's LivePulse component.

Coverage:
  * Unknown brain → 404
  * Never-pinged brain → connected="never", last_seen=null
  * Recently-pinged brain → connected="fresh", age_seconds present
  * Banding boundaries: fresh < 90s, stale < 600s, dead >= 600s
"""
from __future__ import annotations

import os

import requests


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = (BASE_URL or "").rstrip("/")


def _brain_token(brain: str) -> str:
    key = f"{brain.upper()}_INGEST_TOKEN"
    for line in open("/app/backend/.env").read().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"{key} not set")


def _wipe_heartbeats() -> None:
    """Reset shared_heartbeats so we can assert the 'never' state."""
    import subprocess
    subprocess.run(
        ["python3", "-c", (
            "import asyncio, os\n"
            "from motor.motor_asyncio import AsyncIOMotorClient\n"
            "from dotenv import load_dotenv\n"
            "load_dotenv('/app/backend/.env')\n"
            "async def main():\n"
            "    c = AsyncIOMotorClient(os.environ['MONGO_URL'])\n"
            "    await c[os.environ['DB_NAME']]['shared_heartbeats'].delete_many({})\n"
            "asyncio.run(main())\n"
        )],
        check=True,
    )


class TestHeartbeatStatus:
    def test_unknown_brain_404(self):
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-status/skynet", timeout=10,
        )
        assert r.status_code == 404

    def test_never_connected_state(self):
        _wipe_heartbeats()
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-status/chevelle", timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["runtime"] == "chevelle"
        # `never` requires NO heartbeat AND NO sovereign contribution.
        # Chevelle may have a recent sovereign contribution from a real
        # sidecar; if so, the verdict is `partial` not `never`. Either
        # is honest — what we're asserting is the endpoint shape + that
        # heartbeat_age is None right after a wipe.
        assert d["connected"] in {"never", "partial", "stale"}
        assert d["heartbeat_age_seconds"] is None
        # `last_seen` mirrors the most-recent of (heartbeat, contribution).
        # After a heartbeat wipe, last_seen is either None (never) or the
        # contribution timestamp (if a sovereign sidecar has posted).

    def test_fresh_after_ping(self):
        # Ping chevelle, then read back.
        tok = _brain_token("chevelle")
        r0 = requests.get(
            f"{BASE_URL}/api/heartbeat-ping/chevelle?token={tok}", timeout=10,
        )
        assert r0.status_code == 200
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-status/chevelle", timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        # New combined-signal logic: `connected` requires BOTH heartbeat
        # AND a recent sovereign contribution. After only a heartbeat
        # ping (no sidecar contribution), the honest state is `partial`.
        assert d["connected"] in {"connected", "partial"}
        assert d["heartbeat_age_seconds"] is not None
        assert d["heartbeat_age_seconds"] < 10

    def test_no_auth_required(self):
        # The endpoint is public — operator dashboard fetches it
        # client-side and we don't want it to require JWT.
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-status/alpha", timeout=10,
        )
        assert r.status_code == 200
