"""HTTP tests for `/api/admin/paradox/wake*` — operator-issued wake
orders that nudge brains to process a chosen ticker.

Doctrine lock:
  * Wake is admin-only on the issuing side; brains poll with their
    per-runtime ingest token (same as sidecar-checkin).
  * Wake orders carry a signed HS256 JWT envelope. Brains verify it
    against JWT_SECRET before processing.
  * ACK is idempotent — second ack on the same order is a no-op.
  * Cross-brain ack is rejected (brain X cannot ack brain Y's order).
"""
from __future__ import annotations

import os
import time

import jwt as pyjwt
import requests


def _env_var(name: str) -> str:
    v = os.environ.get(name) or ""
    if v:
        return v
    # Fallback: read directly from backend/.env (test runners don't
    # always export every key).
    try:
        with open("/app/backend/.env") as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _alpha_token() -> str:
    return _env_var("ALPHA_INGEST_TOKEN")


def _camaro_token() -> str:
    return _env_var("CAMARO_INGEST_TOKEN")


def _jwt_secret() -> str:
    return _env_var("JWT_SECRET")


# ─────────────────────────── ISSUE ─────────────────────────────────────


def test_wake_requires_admin(base_url, api_client):
    """No bearer → 401/403 on operator-issuing endpoint."""
    r = api_client.post(
        f"{base_url}/api/admin/paradox/wake/alpha",
        json={"ticker": "AAPL"},
        timeout=15,
    )
    assert r.status_code in (401, 403), r.text


def test_wake_rejects_unknown_brain(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/notabrain",
        json={"ticker": "AAPL"},
        timeout=15,
    )
    assert r.status_code == 404


def test_wake_rejects_blank_ticker(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/alpha",
        json={"ticker": "   "},
        timeout=15,
    )
    assert r.status_code == 400


def test_wake_issues_signed_order(base_url, auth_client):
    """Happy path: admin issues a wake order; envelope has a signed
    JWT whose claims match the order."""
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/alpha",
        json={"ticker": "aapl", "note": "premarket gap"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    order = body["order"]
    assert order["brain"] == "alpha"
    assert order["ticker"] == "AAPL"
    assert order["status"] == "pending"
    assert order["note"] == "premarket gap"
    assert order["signed_token"]

    secret = _jwt_secret()
    if secret:
        claims = pyjwt.decode(order["signed_token"], secret, algorithms=["HS256"])
        assert claims["order_id"] == order["order_id"]
        assert claims["brain"] == "alpha"
        assert claims["ticker"] == "AAPL"
        assert claims["kind"] == "wake"


def test_wake_all_fans_out_to_every_live_runtime(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/wake-all",
        json={"ticker": "TSLA"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    brains = {o["brain"] for o in body["orders"]}
    assert brains == {"alpha", "camaro", "chevelle", "redeye"}
    for o in body["orders"]:
        assert o["ticker"] == "TSLA"
        assert o["status"] == "pending"


def test_wake_all_subset_filters_brains(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/wake-all",
        json={"ticker": "MSFT", "brains": ["alpha", "camaro"]},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert {o["brain"] for o in body["orders"]} == {"alpha", "camaro"}


def test_wake_all_subset_rejects_unknown_brain(base_url, auth_client):
    r = auth_client.post(
        f"{base_url}/api/admin/paradox/wake-all",
        json={"ticker": "MSFT", "brains": ["alpha", "ghost"]},
        timeout=15,
    )
    assert r.status_code == 404


# ─────────────────────────── POLL ──────────────────────────────────────


def test_pending_poll_requires_token(base_url, api_client):
    r = api_client.get(
        f"{base_url}/api/admin/paradox/wake-orders/alpha",
        timeout=15,
    )
    assert r.status_code == 401, r.text


def test_pending_poll_returns_issued_order(base_url, auth_client, api_client):
    tok = _alpha_token()
    if not tok:
        return  # env not configured

    issued = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/alpha",
        json={"ticker": "NVDA"},
        timeout=15,
    ).json()["order"]

    r = api_client.get(
        f"{base_url}/api/admin/paradox/wake-orders/alpha",
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["brain"] == "alpha"
    order_ids = [o["order_id"] for o in body["orders"]]
    assert issued["order_id"] in order_ids


# ─────────────────────────── ACK ───────────────────────────────────────


def test_ack_consumes_order_and_is_idempotent(base_url, auth_client, api_client):
    tok = _alpha_token()
    if not tok:
        return

    issued = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/alpha",
        json={"ticker": "AMD"},
        timeout=15,
    ).json()["order"]
    order_id = issued["order_id"]

    r1 = api_client.post(
        f"{base_url}/api/admin/paradox/wake-orders/alpha/{order_id}/ack",
        json={"ack_note": "processed"},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["already_acked"] is False
    assert body1["order"]["status"] == "acked"
    assert body1["order"]["ack_note"] == "processed"

    # Idempotent: second ack is a no-op
    r2 = api_client.post(
        f"{base_url}/api/admin/paradox/wake-orders/alpha/{order_id}/ack",
        json={"ack_note": "re-processed"},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["already_acked"] is True
    # acked_at unchanged
    assert body2["order"]["ack_note"] == "processed"


def test_cross_brain_ack_rejected(base_url, auth_client, api_client):
    """Camaro cannot ack an order targeted at Alpha — even with a valid
    camaro ingest token."""
    alpha_tok = _alpha_token()
    camaro_tok = _camaro_token()
    if not alpha_tok or not camaro_tok:
        return

    issued = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/alpha",
        json={"ticker": "QQQ"},
        timeout=15,
    ).json()["order"]
    order_id = issued["order_id"]

    # Camaro presents its own valid token but addresses /camaro/{order_id}.
    # Endpoint looks the order up and refuses because brain mismatches.
    r = api_client.post(
        f"{base_url}/api/admin/paradox/wake-orders/camaro/{order_id}/ack",
        json={},
        headers={"X-Runtime-Token": camaro_tok},
        timeout=15,
    )
    assert r.status_code == 403, r.text


def test_ack_unknown_order_404(base_url, api_client):
    tok = _alpha_token()
    if not tok:
        return
    r = api_client.post(
        f"{base_url}/api/admin/paradox/wake-orders/alpha/does-not-exist/ack",
        json={},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 404


# ─────────────────────────── ADMIN LIST ────────────────────────────────


def test_admin_list_orders_includes_recent(base_url, auth_client):
    issued = auth_client.post(
        f"{base_url}/api/admin/paradox/wake/redeye",
        json={"ticker": "SPY"},
        timeout=15,
    ).json()["order"]

    r = auth_client.get(
        f"{base_url}/api/admin/paradox/wake-orders?hours=1&limit=20",
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [o["order_id"] for o in body["items"]]
    assert issued["order_id"] in ids
