"""Heartbeat-ping public endpoint tests.

Covers:
  - GET/POST with valid query token → 200 + DB updated
  - X-Runtime-Token header path → 200
  - Bad token → 401
  - Missing token → 401
  - Unknown brain → 404
  - All 4 brains can be pinged with their own tokens (no cross-brain leakage)
  - DB heartbeat row carries detail.source = "heartbeat_ping"
"""
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


def _read_token(brain: str) -> str:
    key = f"{brain.upper()}_INGEST_TOKEN"
    with open("/app/backend/.env") as f:
        for line in f:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"no token in .env for {brain}")


class TestHeartbeatPing:
    def test_get_with_query_token(self):
        tok = _read_token("chevelle")
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-ping/chevelle?token={tok}", timeout=10,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True
        assert d["runtime"] == "chevelle"
        assert d["source"] == "heartbeat_ping"

    def test_post_with_header_token(self):
        tok = _read_token("redeye")
        r = requests.post(
            f"{BASE_URL}/api/heartbeat-ping/redeye",
            headers={"X-Runtime-Token": tok},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["runtime"] == "redeye"

    def test_bad_token_401(self):
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-ping/redeye?token=bogus", timeout=10,
        )
        assert r.status_code == 401

    def test_missing_token_401(self):
        r = requests.get(f"{BASE_URL}/api/heartbeat-ping/redeye", timeout=10)
        assert r.status_code == 401

    def test_unknown_brain_404(self):
        tok = _read_token("redeye")
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-ping/martian?token={tok}", timeout=10,
        )
        assert r.status_code == 404

    def test_cross_brain_token_rejected(self):
        """REDEYE's token must NOT be valid for chevelle's ping endpoint."""
        redeye_tok = _read_token("redeye")
        r = requests.get(
            f"{BASE_URL}/api/heartbeat-ping/chevelle?token={redeye_tok}",
            timeout=10,
        )
        assert r.status_code == 401

    def test_all_four_brains_work(self):
        for brain in ("alpha", "camaro", "chevelle", "redeye"):
            tok = _read_token(brain)
            r = requests.get(
                f"{BASE_URL}/api/heartbeat-ping/{brain}?token={tok}",
                timeout=10,
            )
            assert r.status_code == 200, f"{brain}: {r.text}"
            assert r.json()["runtime"] == brain

    def test_heartbeat_row_tagged_in_db(self):
        """After a ping, the shared/overview endpoint should reflect the
        fresh heartbeat AND the detail.source should be 'heartbeat_ping'."""
        # We can verify the timestamp moved by re-reading overview after
        # ping. Detail-source verification is via a direct DB read.
        tok = _read_token("redeye")
        ping_resp = requests.get(
            f"{BASE_URL}/api/heartbeat-ping/redeye?token={tok}", timeout=10,
        ).json()
        # Direct DB check
        import asyncio
        from pathlib import Path
        env: dict[str, str] = {}
        for raw in Path("/app/backend/.env").read_text().splitlines():
            line = raw.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        os.environ.setdefault("MONGO_URL", env["MONGO_URL"])
        os.environ.setdefault("DB_NAME", env["DB_NAME"])
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])

        async def _check():
            doc = await client[os.environ["DB_NAME"]]["shared_heartbeats"].find_one(
                {"runtime": "redeye"}, {"_id": 0},
            )
            assert doc["last_seen"] == ping_resp["last_seen"]
            assert doc["detail"]["source"] == "heartbeat_ping"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_check())
        finally:
            client.close()
            loop.close()
