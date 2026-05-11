"""Cross-brain discussion layer regression tests.

Verifies:
  - Roles manifest includes all 3 runtimes + REDEYE advisor; may_execute=False.
  - Operator can read/write opinions via JWT.
  - Runtimes can read via X-Runtime-Token (mirror endpoints).
  - Reply threading: child inherits thread_root; depth increments.
  - Schema rejects may_execute=true.
  - Schema rejects malformed topic + invalid stance.
  - Reply to nonexistent opinion returns 404.
  - Token mismatch (alpha token claiming to be camaro) returns 401.
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

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


def _token(env_key: str) -> str:
    """Read a runtime ingest token from backend/.env (string-quoted)."""
    with open("/app/backend/.env") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith(f"{env_key}="):
                v = line.split("=", 1)[1]
                return v.strip().strip('"').strip("'")
    raise RuntimeError(f"{env_key} not in backend/.env")


ALPHA_TOKEN = _token("ALPHA_INGEST_TOKEN")
CAMARO_TOKEN = _token("CAMARO_INGEST_TOKEN")
REDEYE_TOKEN = _token("REDEYE_INGEST_TOKEN")


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}


# ------------------------- Roles manifest -------------------------

class TestRolesManifest:
    def test_operator_view_includes_all_brains(self):
        tok = _login()
        r = requests.get(f"{BASE_URL}/api/shared/roles-manifest", headers=_hdr(tok), timeout=20)
        assert r.status_code == 200, r.text
        d = r.json()
        runtimes = {x["runtime"] for x in d["items"]}
        assert {"alpha", "camaro", "chevelle", "redeye"} <= runtimes
        for x in d["items"]:
            assert x["may_execute"] is False, f"{x['runtime']} must not claim execution"
        # REDEYE is now a full-seat runtime (2026-02-11) — promoted from
        # advisor sidecar. Its authority_state defaults to 'advisor', but
        # kind is 'runtime'.
        redeye = next(x for x in d["items"] if x["runtime"] == "redeye")
        assert redeye["kind"] == "runtime"
        assert redeye["authority_state"] == "advisor"

    def test_runtime_view_via_x_token(self):
        r = requests.get(
            f"{BASE_URL}/api/runtime-discussion/roles-manifest",
            params={"caller": "redeye"},
            headers={"X-Runtime-Token": REDEYE_TOKEN},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        runtimes = {x["runtime"] for x in d["items"]}
        assert {"alpha", "camaro", "chevelle", "redeye"} <= runtimes

    def test_runtime_view_rejects_wrong_token(self):
        r = requests.get(
            f"{BASE_URL}/api/runtime-discussion/roles-manifest",
            params={"caller": "alpha"},
            headers={"X-Runtime-Token": "wrong-token"},
            timeout=20,
        )
        assert r.status_code == 401

    def test_runtime_view_rejects_token_caller_mismatch(self):
        # alpha's token claiming to be camaro must fail
        r = requests.get(
            f"{BASE_URL}/api/runtime-discussion/roles-manifest",
            params={"caller": "camaro"},
            headers={"X-Runtime-Token": ALPHA_TOKEN},
            timeout=20,
        )
        assert r.status_code == 401


# ------------------------- Posting + threading -------------------------

class TestOpinionsPostAndThread:
    def test_post_then_reply_then_fetch_thread(self):
        # Alpha posts a top-level opinion
        r1 = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "alpha",
                "topic": "symbol:AAPL",
                "stance": "long",
                "confidence": 0.7,
                "body": "test root opinion",
                "evidence": {"score": 0.7},
            },
            timeout=20,
        )
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1["depth"] == 0
        assert d1["thread_root"] == d1["opinion_id"]
        root_id = d1["opinion_id"]

        # REDEYE replies
        r2 = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": REDEYE_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "redeye",
                "topic": "symbol:AAPL",
                "stance": "short",
                "confidence": 0.8,
                "body": "test reply",
                "in_reply_to": root_id,
            },
            timeout=20,
        )
        assert r2.status_code == 200, r2.text
        d2 = r2.json()
        assert d2["depth"] == 1
        assert d2["thread_root"] == root_id

        # Fetch thread, must return both messages, oldest first
        tok = _login()
        r3 = requests.get(
            f"{BASE_URL}/api/shared/opinions/{root_id}", headers=_hdr(tok), timeout=20
        )
        assert r3.status_code == 200
        thread = r3.json()
        assert thread["thread_root"] == root_id
        assert thread["count"] >= 2
        ids = [x["opinion_id"] for x in thread["items"]]
        assert root_id in ids
        assert d2["opinion_id"] in ids


# ------------------------- Schema enforcement -------------------------

class TestSchemaRejects:
    def test_rejects_may_execute_true(self):
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "alpha", "topic": "free", "stance": "observation",
                "body": "sneaky", "may_execute": True,
            },
            timeout=20,
        )
        assert r.status_code == 422
        assert "may_execute" in r.text.lower()

    def test_rejects_invalid_stance(self):
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "alpha", "topic": "free", "stance": "EXECUTE",
                "body": "no",
            },
            timeout=20,
        )
        assert r.status_code == 422

    def test_rejects_malformed_topic(self):
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "alpha", "topic": "no-colon-no-prefix", "stance": "observation",
                "body": "no",
            },
            timeout=20,
        )
        assert r.status_code == 422

    def test_reply_to_nonexistent_returns_404(self):
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "alpha", "topic": "free", "stance": "observation",
                "body": "ghost reply",
                "in_reply_to": "00000000-0000-0000-0000-000000000000",
            },
            timeout=20,
        )
        assert r.status_code == 404

    def test_runtime_field_must_match_token(self):
        # Alpha's token cannot post as Camaro
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "camaro", "topic": "free", "stance": "observation",
                "body": "impersonation attempt",
            },
            timeout=20,
        )
        assert r.status_code == 401


# ------------------------- Reading filters -------------------------

class TestReadingFilters:
    def test_runtime_filter_works(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"runtime": "redeye", "limit": 50},
            headers=_hdr(tok),
            timeout=20,
        )
        assert r.status_code == 200
        for x in r.json()["items"]:
            assert x["runtime"] == "redeye"

    def test_symbol_filter_works(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"symbol": "AAPL", "limit": 50},
            headers=_hdr(tok),
            timeout=20,
        )
        assert r.status_code == 200
        for x in r.json()["items"]:
            assert x["topic"] == "symbol:AAPL"

    def test_invalid_runtime_filter_400(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"runtime": "ghost"},
            headers=_hdr(tok),
            timeout=20,
        )
        assert r.status_code == 400
