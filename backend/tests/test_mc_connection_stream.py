"""Regression tests for `/api/mc-connection/stream` (SSE).

Doctrine pin (2026-06-10, P2): the dashboard's live observability
strip depends on this endpoint. These tests pin:
  * Auth via query-param token (browser EventSource limitation).
  * Auth via Authorization header (curl / server-side clients).
  * `hello` event lands within the first poll cycle.
  * `intent` events fire when new rows land in `shared_intents`.
  * Unauthenticated requests are rejected.

Run against the live backend — SSE testing requires a real ASGI
server.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or "http://localhost:8001"


def _get_token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={
            "email": "admin@risedual.io",
            "password": "risedual-admin-2026",
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("access_token") or body.get("token")


def _read_sse_events(resp, max_events: int = 5, max_seconds: float = 10.0):
    """Parse an SSE stream up to `max_events` named events or
    `max_seconds`. Returns [(event_name, parsed_data_dict), ...].

    Doctrine (2026-06-10): use `iter_content(chunk_size=1)` for SSE
    because `requests.iter_lines()` waits for a 512-byte chunk to
    accumulate before yielding — fine for batch APIs but breaks SSE
    where each event is < 500 bytes. Streaming one byte at a time
    and splitting on `\\n` ourselves is the canonical fix.
    """
    import time
    events: list[tuple[str, dict]] = []
    started = time.monotonic()
    buf = ""
    current_event = None
    try:
        for chunk in resp.iter_content(chunk_size=1, decode_unicode=True):
            if time.monotonic() - started > max_seconds:
                break
            if not chunk:
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line == "":
                    # SSE event boundary — reset current_event so the
                    # next block starts clean.
                    current_event = None
                    continue
                if line.startswith(":"):
                    continue  # comment / ping
                if line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_str = line[len("data:"):].strip()
                    if current_event:
                        try:
                            payload = json.loads(data_str)
                        except json.JSONDecodeError:
                            payload = {"raw": data_str}
                        events.append((current_event, payload))
            if len(events) >= max_events:
                break
    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError):
        # Stream closed mid-read — return whatever we got.
        pass
    return events


@pytest.fixture(scope="module")
def _token():
    return _get_token()


def test_sse_requires_auth():
    """No token → 401."""
    r = requests.get(
        f"{BASE_URL}/api/mc-connection/stream", timeout=5,
        # Don't stream — just hit the endpoint
    )
    assert r.status_code in (401, 403), (
        f"unauthenticated SSE must be rejected, got {r.status_code}"
    )


def test_sse_rejects_bad_token():
    r = requests.get(
        f"{BASE_URL}/api/mc-connection/stream",
        params={"token": "not.a.real.token"}, timeout=5,
    )
    assert r.status_code == 401


def test_sse_hello_event_first(_token):
    """First message on the stream must be a `hello` event with the
    expected metadata shape."""
    with requests.get(
        f"{BASE_URL}/api/mc-connection/stream",
        params={"token": _token},
        stream=True,
        timeout=12,
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("Content-Type", "")
        events = _read_sse_events(r, max_events=1, max_seconds=5)
    assert len(events) >= 1
    name, payload = events[0]
    assert name == "hello"
    assert "ts" in payload
    assert "poll_interval_sec" in payload


def test_sse_streams_named_events(_token):
    """A few seconds of stream output should include at least the
    `hello` event and (with 4 brains posting continuously) at least
    one `intent` event.

    Doctrine (2026-06-10): bumped from 15s to 20s window after
    observing 12s yielded only the hello event in pytest's slower
    environment (vs ~6 events in a bare python repro). Brains run
    one tick per ~45s — 20s catches at least 1 tick on average.
    """
    with requests.get(
        f"{BASE_URL}/api/mc-connection/stream",
        params={"token": _token},
        stream=True,
        timeout=25,
    ) as r:
        events = _read_sse_events(r, max_events=10, max_seconds=20)
    names = [n for n, _ in events]
    assert "hello" in names, f"missing hello; got {names}"
    # With 4 brains running we expect at least one intent in 20s.
    # Soft assertion — if the brains happen to be idle, just require
    # any non-hello event landed.
    assert any(n != "hello" for n in names), (
        f"stream should produce intents/regime/heartbeat events in 20s; "
        f"got only {names}"
    )


def test_sse_bearer_header_also_works(_token):
    """Servers that can set Authorization (curl, server-side clients)
    must still be accepted even without `?token=`."""
    with requests.get(
        f"{BASE_URL}/api/mc-connection/stream",
        headers={"Authorization": f"Bearer {_token}"},
        stream=True,
        timeout=8,
    ) as r:
        assert r.status_code == 200
        events = _read_sse_events(r, max_events=1, max_seconds=4)
    assert len(events) >= 1
    assert events[0][0] == "hello"


def test_sse_intent_event_fires_on_new_row(_token):
    """Seed a synthetic intent into `shared_intents` and verify it
    surfaces on the stream within the poll window.

    Doctrine (2026-06-10): use ONE `_read_sse_events` call rather
    than two so the parser's internal `buf` doesn't lose bytes
    between calls. The buf-discard bug burned us during initial
    testing — pin the single-call pattern here for posterity.
    """
    import pymongo
    c = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    try:
        # Open the stream and inject the test row WHILE the stream
        # is open so the poller catches it on its next cycle. Inject
        # in a background thread so we can keep reading.
        import threading
        injected = threading.Event()

        def _inject():
            # Small delay so the stream's `initial_ts` is in the past
            # by the time we insert.
            import time as _t
            _t.sleep(1.5)
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            db.shared_intents.insert_one({
                "intent_id": f"sse-test-{now_iso}",
                "stack": "sse_test_brain",
                "action": "HOLD",
                "symbol": "TEST",
                "lane": "equity",
                "confidence": 0.42,
                "gate_state": "pending",
                "ingest_ts": now_iso,
            })
            injected.set()

        t = threading.Thread(target=_inject, daemon=True)
        t.start()

        with requests.get(
            f"{BASE_URL}/api/mc-connection/stream",
            params={"token": _token},
            stream=True,
            timeout=20,
        ) as r:
            assert r.status_code == 200
            events = _read_sse_events(r, max_events=50, max_seconds=12)

        assert injected.is_set(), "background inject thread did not run"
        saw_test_intent = False
        for name, payload in events:
            if name == "intent" and payload.get("stack") == "sse_test_brain":
                saw_test_intent = True
                assert payload.get("symbol") == "TEST"
                assert payload.get("action") == "HOLD"
                break
        assert saw_test_intent, (
            f"injected intent did not surface on stream; "
            f"got {len(events)} events: {[n for n, _ in events]}"
        )
    finally:
        db.shared_intents.delete_many({"stack": "sse_test_brain"})
        c.close()


def test_sse_position_misread_event_fires_on_new_row(_token):
    """Seed a synthetic position-misread into `shared_position_misreads`
    and verify it surfaces on the SSE stream as a `position_misread`
    event. The frontend toast host (`MisreadToastHost.jsx`) subscribes
    to exactly this event — losing it would silently break the
    operator's last fire-alarm against the 2026-06-09 AAPL pattern.
    """
    import pymongo
    import threading
    import time as _t
    c = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    test_symbol = f"TST{int(_t.time())}"
    try:
        injected = threading.Event()

        def _inject():
            _t.sleep(1.5)
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            db.shared_position_misreads.insert_one({
                "detected_at": now_iso,
                "symbol": test_symbol,
                "brain": "sse_test_brain",
                "assumed_side": "flat",
                "actual_side": "short",
                "emitted_action": "BUY",
                "actual_signed_qty": -10.0,
                "missed_short_profit": True,
            })
            injected.set()

        t = threading.Thread(target=_inject, daemon=True)
        t.start()

        with requests.get(
            f"{BASE_URL}/api/mc-connection/stream",
            params={"token": _token},
            stream=True,
            timeout=20,
        ) as r:
            assert r.status_code == 200
            events = _read_sse_events(r, max_events=80, max_seconds=12)

        assert injected.is_set(), "background inject thread did not run"
        saw_misread = False
        for name, payload in events:
            if name == "position_misread" and payload.get("symbol") == test_symbol:
                saw_misread = True
                # The toast host reads these specific fields — pin
                # the contract so future schema changes can't quietly
                # break it.
                assert payload.get("brain") == "sse_test_brain"
                assert payload.get("assumed_side") == "flat"
                assert payload.get("actual_side") == "short"
                assert payload.get("emitted_action") == "BUY"
                assert payload.get("missed_short_profit") is True
                break
        assert saw_misread, (
            f"injected position_misread did not surface on stream; "
            f"got events: {[n for n, _ in events]}"
        )
    finally:
        db.shared_position_misreads.delete_many({"brain": "sse_test_brain"})
        c.close()
