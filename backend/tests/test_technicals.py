"""Shared Technical Evidence layer — backend regression tests.

Verifies:
  - Indicator math is correct on a known fixture (SMA / EMA / RSI / MACD).
  - `/api/ingest/ohlcv` requires a valid feeder token (401 on missing/bad,
    400 on unknown source).
  - Bars are idempotent on (source, symbol, tf, ts) — re-ingest of the
    same bar updates rather than duplicates.
  - Batch ingest fans out one snapshot recompute per (symbol, tf) pair.
  - Operator and runtime read endpoints return the same shape.
  - Runtime endpoint rejects a token / caller mismatch (401).
  - Unknown symbol returns 404.
"""
import math
import os
import time
from datetime import datetime, timedelta, timezone
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


def _env(key: str) -> str:
    with open("/app/backend/.env") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"{key} missing")


KRAKEN_TOKEN = _env("KRAKEN_FEEDER_TOKEN")
TOS_TOKEN = _env("TOS_FEEDER_TOKEN")
ALPHA_TOKEN = _env("ALPHA_INGEST_TOKEN")
CAMARO_TOKEN = _env("CAMARO_INGEST_TOKEN")


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


def _bar(symbol, ts, c, *, source="manual", tf="1h", o=None, h=None, l=None, v=1000.0):
    return {
        "source": source, "symbol": symbol, "tf": tf, "ts": ts,
        "o": o if o is not None else c, "h": h if h is not None else c * 1.01,
        "l": l if l is not None else c * 0.99, "c": c, "v": v,
    }


def _hour_series(start_iso: str, n: int) -> list[str]:
    """Return n consecutive hourly ISO timestamps starting at start_iso."""
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    return [(start + timedelta(hours=i)).isoformat() for i in range(n)]


# ───────────────────────── indicator math ─────────────────────────

class TestIndicators:
    def test_sma_known_fixture(self):
        from shared.indicators import sma
        # SMA(3) of [1,2,3,4,5] = [None, None, 2.0, 3.0, 4.0]
        out = sma([1, 2, 3, 4, 5], 3)
        assert out == [None, None, 2.0, 3.0, 4.0]

    def test_ema_classic_smoothing(self):
        from shared.indicators import ema
        # EMA(3) seeded by SMA(3) of first 3 values, then k=2/(3+1)=0.5
        # series [10, 20, 30, 40, 50]:
        #   seed = (10+20+30)/3 = 20.0
        #   step 4: (40-20)*0.5 + 20 = 30.0
        #   step 5: (50-30)*0.5 + 30 = 40.0
        out = ema([10, 20, 30, 40, 50], 3)
        assert out[0] is None and out[1] is None
        assert out[2] == 20.0 and out[3] == 30.0 and out[4] == 40.0

    def test_rsi_monotonic_up_returns_100(self):
        from shared.indicators import rsi
        out = rsi(list(range(1, 30)), 14)
        # All gains, no losses → RSI = 100.
        assert out[14] == 100.0
        assert out[-1] == 100.0

    def test_rsi_monotonic_down_returns_zero(self):
        from shared.indicators import rsi
        out = rsi(list(range(30, 1, -1)), 14)
        # All losses, no gains → RSI = 0.
        assert out[14] == 0.0
        assert out[-1] == 0.0

    def test_macd_aligns_with_emas(self):
        from shared.indicators import macd, ema
        prices = [float(x) for x in range(1, 60)]
        out = macd(prices, 12, 26, 9)
        # MACD line at last index should equal EMA12 - EMA26.
        e12 = ema(prices, 12)[-1]
        e26 = ema(prices, 26)[-1]
        assert out["macd"][-1] is not None
        assert math.isclose(out["macd"][-1], e12 - e26, rel_tol=1e-9)
        # Signal is computed once at least 9 valid MACD values exist.
        assert out["signal"][-1] is not None

    def test_bollinger_width(self):
        from shared.indicators import bollinger
        # Constant series → SD = 0, width = 0.
        out = bollinger([100.0] * 25, 20, 2.0)
        assert out["upper"][-1] == 100.0
        assert out["lower"][-1] == 100.0
        assert out["width_pct"][-1] == 0.0

    def test_build_snapshot_minimum_bars(self):
        from shared.indicators import build_snapshot
        # 1 bar — ready=True but most indicators None.
        snap = build_snapshot([{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
        assert snap["ready"] is True
        assert snap["bars_seen"] == 1
        assert snap["rsi14"] is None
        assert snap["sma"]["20"] is None


# ───────────────────────── feeder auth ─────────────────────────

class TestFeederAuth:
    def test_unknown_source_400(self):
        bar = _bar("XAUUSD", "2025-01-01T00:00:00+00:00", 100.0, source="bloomberg")
        r = requests.post(f"{BASE_URL}/api/ingest/ohlcv", json=bar, timeout=20)
        # Pydantic Literal rejects "bloomberg" → 422 before our 400 path
        # (either is acceptable; assert NOT a 200).
        assert r.status_code in (400, 422), r.text

    def test_missing_token_401(self):
        bar = _bar("BTC/USD", "2025-01-01T00:00:00+00:00", 50000.0, source="kraken_pro")
        r = requests.post(f"{BASE_URL}/api/ingest/ohlcv", json=bar, timeout=20)
        assert r.status_code == 401

    def test_wrong_token_401(self):
        bar = _bar("BTC/USD", "2025-01-01T00:00:00+00:00", 50000.0, source="kraken_pro")
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv",
            headers={"X-Feeder-Token": "garbage", "Content-Type": "application/json"},
            json=bar, timeout=20,
        )
        assert r.status_code == 401

    def test_wrong_token_for_source_401(self):
        # Send a Kraken bar with the TOS token → 401
        bar = _bar("BTC/USD", "2025-01-01T01:00:00+00:00", 50000.0, source="kraken_pro")
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json=bar, timeout=20,
        )
        assert r.status_code == 401


# ───────────────────────── ingest flow ─────────────────────────

class TestIngestAndRead:
    def test_single_bar_then_snapshot_increments(self):
        sym = f"TST{int(time.time()) % 100000}"
        ts_series = _hour_series("2025-01-01T00:00:00+00:00", 5)
        for i, (ts, c) in enumerate(zip(ts_series, [100, 101, 102, 103, 104])):
            r = requests.post(
                f"{BASE_URL}/api/ingest/ohlcv",
                headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
                json=_bar(sym, ts, float(c), source="thinkorswim"),
                timeout=20,
            )
            assert r.status_code == 200, r.text
            assert r.json()["bars_seen"] == i + 1

    def test_bar_idempotency(self):
        sym = f"IDM{int(time.time()) % 100000}"
        ts = "2025-02-01T00:00:00+00:00"
        bar = _bar(sym, ts, 200.0, source="thinkorswim")
        r1 = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json=bar, timeout=20,
        )
        assert r1.status_code == 200
        # Same key, revised close — still 200, snapshot count unchanged.
        r2 = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json={**bar, "c": 205.0}, timeout=20,
        )
        assert r2.status_code == 200
        assert r2.json()["bars_seen"] == 1

    def test_batch_ingest(self):
        sym = f"BAT{int(time.time()) % 100000}"
        ts_series = _hour_series("2025-03-01T00:00:00+00:00", 30)
        bars = [
            _bar(sym, ts, 50.0 + i, source="kraken_pro")
            for i, ts in enumerate(ts_series)
        ]
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv/batch",
            headers={"X-Feeder-Token": KRAKEN_TOKEN, "Content-Type": "application/json"},
            json={"bars": bars}, timeout=30,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ingested"] == 30
        assert any(s["symbol"] == sym and s["bars_seen"] == 30 for s in d["snapshots"])

    def test_batch_mixed_source_400(self):
        bars = [
            _bar("BTC/USD", "2025-04-01T00:00:00+00:00", 50000.0, source="kraken_pro"),
            _bar("NVDA", "2025-04-01T00:00:00+00:00", 100.0, source="thinkorswim"),
        ]
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv/batch",
            headers={"X-Feeder-Token": KRAKEN_TOKEN, "Content-Type": "application/json"},
            json={"bars": bars}, timeout=20,
        )
        assert r.status_code == 400

    def test_operator_read_returns_shape(self):
        sym = f"OPR{int(time.time()) % 100000}"
        ts_series = _hour_series("2025-05-01T00:00:00+00:00", 30)
        bars = [
            _bar(sym, ts, 75.0 + i * 0.5, source="thinkorswim")
            for i, ts in enumerate(ts_series)
        ]
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv/batch",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json={"bars": bars}, timeout=30,
        )
        assert r.status_code == 200

        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/technical/{sym}",
            params={"tf": "1h"}, headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["symbol"] == sym
        assert d["tf"] == "1h"
        assert d["snapshot"]["indicators"]["ready"] is True
        # 30 bars: SMA(20) ready, SMA(50) not yet.
        assert d["snapshot"]["indicators"]["sma"]["20"] is not None
        assert d["snapshot"]["indicators"]["sma"]["50"] is None

    def test_operator_read_404_on_unknown_symbol(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/technical/NONEXISTENTXYZ",
            params={"tf": "1h"}, headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 404

    def test_runtime_read_works_with_runtime_token(self):
        sym = f"RTR{int(time.time()) % 100000}"
        ts_series = _hour_series("2025-06-01T00:00:00+00:00", 25)
        bars = [
            _bar(sym, ts, 25.0 + i, source="kraken_pro")
            for i, ts in enumerate(ts_series)
        ]
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv/batch",
            headers={"X-Feeder-Token": KRAKEN_TOKEN, "Content-Type": "application/json"},
            json={"bars": bars}, timeout=30,
        )
        assert r.status_code == 200

        r = requests.get(
            f"{BASE_URL}/api/runtime-discussion/technical/{sym}",
            params={"caller": "alpha", "tf": "1h"},
            headers={"X-Runtime-Token": ALPHA_TOKEN},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        assert r.json()["symbol"] == sym

    def test_runtime_read_token_mismatch_401(self):
        r = requests.get(
            f"{BASE_URL}/api/runtime-discussion/technical/NVDA",
            params={"caller": "camaro", "tf": "1h"},
            headers={"X-Runtime-Token": ALPHA_TOKEN},  # alpha token but claims camaro
            timeout=20,
        )
        assert r.status_code == 401

    def test_symbols_endpoint_includes_recent_ingest(self):
        sym = f"UNI{int(time.time()) % 100000}"
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json=_bar(sym, "2025-07-01T00:00:00+00:00", 1.0, source="thinkorswim"),
            timeout=20,
        )
        assert r.status_code == 200

        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/technical/symbols",
            headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["symbol"] == sym and it["source"] == "thinkorswim" for it in items)

    def test_replay_at_past_timestamp(self):
        """Indicator recompute from bars ≤ as_of — confirms the audit
        path returns historical (not live) values."""
        sym = f"RPL{int(time.time()) % 100000}"
        ts_series = _hour_series("2025-08-01T00:00:00+00:00", 30)
        # Strictly rising price series — old snapshot close < new.
        bars = [
            _bar(sym, ts, 50.0 + i, source="thinkorswim")
            for i, ts in enumerate(ts_series)
        ]
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv/batch",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json={"bars": bars}, timeout=30,
        )
        assert r.status_code == 200

        tok = _login()
        # Live snapshot — should see the most recent close (50 + 29 = 79).
        r_live = requests.get(
            f"{BASE_URL}/api/shared/technical/{sym}",
            params={"tf": "1h"}, headers=_hdr(tok), timeout=20,
        )
        assert r_live.status_code == 200
        live_close = r_live.json()["snapshot"]["indicators"]["last_close"]
        assert live_close == 79.0
        assert r_live.json()["replayed"] is False

        # Replay at hour 10 — should see close = 50+10 = 60.
        as_of = ts_series[10]
        r_replay = requests.get(
            f"{BASE_URL}/api/shared/technical/{sym}",
            params={"tf": "1h", "as_of": as_of}, headers=_hdr(tok), timeout=20,
        )
        assert r_replay.status_code == 200, r_replay.text
        d = r_replay.json()
        assert d["replayed"] is True
        assert d["as_of"] == as_of
        replay_close = d["snapshot"]["indicators"]["last_close"]
        assert replay_close == 60.0
        # And the replayed close is strictly less than the live one,
        # proving we're truly recomputing as-of.
        assert replay_close < live_close

    def test_replay_404_when_no_bars_before_as_of(self):
        tok = _login()
        sym = f"RP4{int(time.time()) % 100000}"
        # Ingest one bar in 2025
        r = requests.post(
            f"{BASE_URL}/api/ingest/ohlcv",
            headers={"X-Feeder-Token": TOS_TOKEN, "Content-Type": "application/json"},
            json=_bar(sym, "2025-09-01T00:00:00+00:00", 100.0, source="thinkorswim"),
            timeout=20,
        )
        assert r.status_code == 200
        # Ask for replay BEFORE the bar exists.
        r = requests.get(
            f"{BASE_URL}/api/shared/technical/{sym}",
            params={"tf": "1h", "as_of": "2024-01-01T00:00:00+00:00"},
            headers=_hdr(tok), timeout=20,
        )
        assert r.status_code == 404
