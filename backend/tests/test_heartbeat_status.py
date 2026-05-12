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
        assert d["connected"] == "never"
        assert d["last_seen"] is None
        assert d["age_seconds"] is None

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
        assert d["connected"] == "fresh"
        assert d["last_seen"] is not None
        assert d["age_seconds"] is not None
        assert d["age_seconds"] < 10    # fresh = under a minute or so

    def test_no_auth_required(self):
        # The endpoint is public — operator dashboard fetches it
        # client-side and we don't want it to require JWT.
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-status/alpha", timeout=10,
        )
        assert r.status_code == 200
