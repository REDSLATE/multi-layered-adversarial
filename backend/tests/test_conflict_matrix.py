"""Heat-map matrix backend tests.

GET /api/shared/conflicts/matrix aggregates ALL pair combinations into
a single payload — replaces N round-trips for the dashboard. Tests:
  - response shape (6 cells for 4 brains, all required keys present)
  - heat band is one of cold/cool/warm/hot/blazing
  - JWT auth required
  - cell counts match a per-pair scorecard for one sampled pair
    (sanity-check the aggregator computes the same as the per-pair
    endpoint already in use by the dashboard)
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


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


class TestConflictMatrix:
    def test_matrix_shape(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/conflicts/matrix",
            headers=_hdr(tok),
            timeout=15,
        )
        assert r.status_code == 200
        d = r.json()
        assert set(d["brains"]) == {"alpha", "camaro", "chevelle", "redeye"}
        # 4 brains → 6 unique unordered pairs.
        assert len(d["cells"]) == 6
        seen_pairs = set()
        for c in d["cells"]:
            assert c["a"] != c["b"]
            pair = frozenset([c["a"], c["b"]])
            assert pair not in seen_pairs, "duplicate pair in matrix"
            seen_pairs.add(pair)
            for k in ("decisive", "a_wins", "b_wins", "temperature", "heat"):
                assert k in c
            for w in ("24h", "7d", "30d"):
                assert w in c["temperature"]
                for k in ("conflicts", "decisive", "stale_or_open"):
                    assert k in c["temperature"][w]
            assert c["heat"] in ("cold", "cool", "warm", "hot", "blazing")
            # win rates are either both None (no decisive yet) or both numeric
            if c["decisive"] == 0:
                assert c["a_win_rate"] is None
                assert c["b_win_rate"] is None
            else:
                assert 0.0 <= c["a_win_rate"] <= 1.0
                assert 0.0 <= c["b_win_rate"] <= 1.0
                # Decisive wins should sum to decisive count
                assert c["a_wins"] + c["b_wins"] == c["decisive"]

    def test_matrix_auth_required(self):
        r = requests.get(f"{BASE_URL}/api/shared/conflicts/matrix", timeout=10)
        assert r.status_code in (401, 403)

    def test_matrix_agrees_with_pair_scorecard(self):
        """For one sampled pair, decisive + a_wins + 7d friction should
        match exactly what the per-pair endpoint reports."""
        tok = _login()
        m = requests.get(
            f"{BASE_URL}/api/shared/conflicts/matrix",
            headers=_hdr(tok),
            timeout=15,
        ).json()
        # Pick the cell with most conflicts so we have non-zero numbers
        # to compare. Default to first cell if all are zero.
        cell = max(
            m["cells"],
            key=lambda c: c["temperature"]["7d"]["conflicts"],
            default=m["cells"][0],
        )
        scorecard = requests.get(
            f"{BASE_URL}/api/shared/conflicts/pair-scorecard?a={cell['a']}&b={cell['b']}",
            headers=_hdr(tok),
            timeout=15,
        ).json()
        assert scorecard["decisive"] == cell["decisive"]
        assert scorecard["a_wins"] == cell["a_wins"]
        assert scorecard["b_wins"] == cell["b_wins"]
        # 7d friction count must match
        assert (
            scorecard["temperature"]["7d"]["conflicts"]
            == cell["temperature"]["7d"]["conflicts"]
        )
        # Heat band must match too (computed from same 7d count)
        assert scorecard["heat"] == cell["heat"]
