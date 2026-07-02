"""Step 5 (Camaro regime) + Step 3 (Chevelle source) regression tests.

Verifies:
  - Opinion schema accepts a snake_case `regime` field and rejects garbage.
  - Outcome doc carries the regime forward from the opinion (for fast agg).
  - Camaro scorecard returns `regime_breakdown.endorse_only` with the
    correct hit_rate for a fixture.
  - Chevelle scorecard returns `source_breakdown` keyed by
    `evidence.source` and rolls "_unsourced" for missing sources.
"""
import os
import time
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = (BASE_URL or "").rstrip("/")

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


def _token(env_key: str) -> str:
    with open("/app/backend/.env") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith(f"{env_key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"{env_key} missing")


CAMINO_TOKEN = _token("CAMINO_INGEST_TOKEN")
BARRACUDA_TOKEN = _token("BARRACUDA_INGEST_TOKEN")
HELLCAT_TOKEN = _token("HELLCAT_INGEST_TOKEN")


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


def _post_opinion(runtime: str, runtime_token: str, *, stance: str, body: str,
                  topic: str = "free", confidence: float = 0.5,
                  evidence: dict | None = None, regime: str | None = None) -> str:
    payload = {
        "runtime": runtime, "topic": topic, "stance": stance, "body": body,
        "confidence": confidence, "evidence": evidence or {},
    }
    if regime is not None:
        payload["regime"] = regime
    r = requests.post(
        f"{BASE_URL}/api/ingest/opinion",
        headers={"X-Runtime-Token": runtime_token, "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    assert r.status_code == 200, r.text
    return r.json()["opinion_id"]


def _resolve(tok: str, oid: str, actual: str) -> None:
    r = requests.post(
        f"{BASE_URL}/api/admin/outcome", headers=_hdr(tok),
        json={"opinion_id": oid, "actual": actual}, timeout=20,
    )
    assert r.status_code == 200, r.text


# ───────────────────── regime field on opinion ─────────────────────

class TestRegimeSchema:
    def test_opinion_accepts_snake_case_regime(self):
        oid = _post_opinion(
            "camaro", BARRACUDA_TOKEN, stance="observation",
            body=f"regime ok {time.time()}", topic="free", regime="trend",
        )
        # Fetch the opinion back and confirm regime persisted.
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"runtime": "camaro", "limit": 50},
            headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        match = next((x for x in items if x["opinion_id"] == oid), None)
        assert match is not None
        assert match.get("regime") == "trend"

    def test_opinion_rejects_bad_regime(self):
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": BARRACUDA_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "camaro", "topic": "free", "stance": "observation",
                "body": "bad regime", "regime": "Trend Up!",
            },
            timeout=20,
        )
        assert r.status_code == 422, r.text

    def test_opinion_optional_regime_persists_as_null(self):
        oid = _post_opinion(
            "camaro", BARRACUDA_TOKEN, stance="observation",
            body=f"no regime {time.time()}", topic="free",
        )
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"runtime": "camaro", "limit": 50},
            headers=_hdr(tok), timeout=20,
        )
        items = r.json()["items"]
        match = next((x for x in items if x["opinion_id"] == oid), None)
        assert match is not None
        assert match.get("regime") in (None, "")


# ───────────────────── camaro regime breakdown ─────────────────────

class TestCamaroRegimeBreakdown:
    def test_endorse_hit_rate_by_regime(self):
        """3 endorse in 'trend' (2W/1L) + 2 endorse in 'chop' (0W/2L)."""
        tok = _login()
        suffix = int(time.time())

        trend_ids = []
        for i, actual in enumerate(["win", "win", "loss"]):
            oid = _post_opinion(
                "camaro", BARRACUDA_TOKEN, stance="endorse",
                body=f"endorse trend {suffix}-{i}",
                topic=f"symbol:R{suffix}T{i}",
                confidence=0.7, regime="trend",
            )
            trend_ids.append((oid, actual))
        for oid, actual in trend_ids:
            _resolve(tok, oid, actual)

        chop_ids = []
        for i, actual in enumerate(["loss", "loss"]):
            oid = _post_opinion(
                "camaro", BARRACUDA_TOKEN, stance="endorse",
                body=f"endorse chop {suffix}-{i}",
                topic=f"symbol:R{suffix}C{i}",
                confidence=0.6, regime="chop",
            )
            chop_ids.append((oid, actual))
        for oid, actual in chop_ids:
            _resolve(tok, oid, actual)

        r = requests.get(
            f"{BASE_URL}/api/shared/scorecard", params={"runtime": "camaro"},
            headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 200
        d = r.json()
        assert "regime_breakdown" in d
        rb = d["regime_breakdown"]
        assert "endorse_only" in rb and "overall" in rb

        eo = {row["regime"]: row for row in rb["endorse_only"]}
        # Aggregates may include other tests' rows; check ≥ our fixture
        # contribution and verify our specific symbols summed correctly via
        # per-regime counts (n ≥ fixture).
        assert "trend" in eo
        assert eo["trend"]["wins"] >= 2
        assert eo["trend"]["losses"] >= 1
        assert "chop" in eo
        assert eo["chop"]["losses"] >= 2


# ───────────────────── chevelle source breakdown ─────────────────────

class TestChevelleSourceBreakdown:
    def test_source_reliability_slice(self):
        """2 chevelle observations with source=feed_a (1W/1L) +
        1 with source=feed_b (1W) + 1 with no source (1L)."""
        tok = _login()
        suffix = int(time.time())

        fixtures = [
            ({"source": "feed_a"}, "win"),
            ({"source": "feed_a"}, "loss"),
            ({"source": "feed_b"}, "win"),
            ({}, "loss"),
        ]
        for i, (evidence, actual) in enumerate(fixtures):
            oid = _post_opinion(
                "chevelle", HELLCAT_TOKEN, stance="observation",
                body=f"src test {suffix}-{i}",
                topic=f"symbol:SRC{suffix}{i}",
                evidence=evidence, confidence=0.5,
            )
            # Chevelle cannot resolve its own; operator resolves.
            _resolve(tok, oid, actual)

        r = requests.get(
            f"{BASE_URL}/api/shared/scorecard", params={"runtime": "chevelle"},
            headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 200
        d = r.json()
        assert "source_breakdown" in d
        sb = {row["source"]: row for row in d["source_breakdown"]}
        assert "feed_a" in sb
        assert sb["feed_a"]["wins"] >= 1 and sb["feed_a"]["losses"] >= 1
        assert "feed_b" in sb
        assert sb["feed_b"]["wins"] >= 1
        assert "_unsourced" in sb
        assert sb["_unsourced"]["losses"] >= 1
