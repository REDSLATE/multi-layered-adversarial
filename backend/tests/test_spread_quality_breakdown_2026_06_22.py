"""Regression: /api/admin/spread-quality/breakdown endpoint.

Operator pin (2026-06-22):
    "That turns this from a hidden data poison issue into an
    observable feed-health signal."

Pins:
  * Global totals across the three quality buckets (live/stale/sentinel)
  * Per-symbol counts + ranked by combined untrusted (stale+sentinel)
  * `untagged_pre_patch` counter exposes how much of the window
    predates the spread-quality fix (legacy intents have no tag)
  * `top` query bound [1, 500], `hours` query bound [1, 168]
  * `lane` query restricts to equity/crypto/all
  * READ-ONLY — endpoint must never write to shared_intents
"""
from __future__ import annotations

import sys
import pytest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")


def _intent(symbol: str, quality: str | None, *, lane: str = "equity",
            minutes_ago: int = 5) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    snap = {}
    if quality is not None:
        snap["spread_quality"] = quality
    return {
        "intent_id": f"i-{symbol}-{quality}-{minutes_ago}",
        "symbol": symbol,
        "lane": lane,
        "ingest_ts": ts,
        "snapshot": snap,
    }


def _make_db(intent_rows):
    class _Cursor:
        def __init__(self, rows): self._rows = rows
        def __aiter__(self): return self._gen()
        async def _gen(self):
            for r in self._rows: yield r

    class _Coll:
        def __init__(self, rows): self._rows = rows
        def find(self, query, projection=None):
            lane = (query or {}).get("lane")
            if lane:
                rows = [r for r in self._rows if r.get("lane") == lane]
            else:
                rows = list(self._rows)
            return _Cursor(rows)

    class _DB:
        def __init__(self, rows): self._rows = rows
        def __getitem__(self, name): return _Coll(self._rows)

    return _DB(intent_rows)


@pytest.mark.asyncio
async def test_global_totals_and_per_symbol_breakdown(monkeypatch):
    """Smoke: aggregate counts by quality, both globally and per
    symbol. Verify the locked response schema."""
    from routes import admin_spread_quality as mod

    rows = [
        # NVDA — healthy feed
        _intent("NVDA", "live"),
        _intent("NVDA", "live", minutes_ago=10),
        _intent("NVDA", "live", minutes_ago=15),
        _intent("NVDA", "stale", minutes_ago=20),
        # A (Agilent) — poison feed
        _intent("A", "sentinel"),
        _intent("A", "sentinel", minutes_ago=10),
        _intent("A", "sentinel", minutes_ago=15),
        _intent("A", "stale", minutes_ago=20),
        _intent("A", "stale", minutes_ago=25),
        # AAPL — mixed
        _intent("AAPL", "live"),
        _intent("AAPL", "sentinel", minutes_ago=10),
        # pre-patch row (no quality tag)
        _intent("XYZQ", None, minutes_ago=30),
    ]

    monkeypatch.setattr(mod, "db", _make_db(rows))

    resp = await mod.spread_quality_breakdown(
        hours=24, top=25, lane="equity", _user={"sub": "test"},
    )

    # Schema lock
    for key in ("hours", "lane", "totals", "untagged_pre_patch",
                "intents_observed", "by_symbol", "fetched_at"):
        assert key in resp, f"missing key {key!r}"

    # Global totals
    assert resp["totals"]["live"] == 4    # NVDA x3, AAPL x1
    assert resp["totals"]["stale"] == 3   # NVDA x1, A x2
    assert resp["totals"]["sentinel"] == 4  # A x3, AAPL x1
    assert resp["untagged_pre_patch"] == 1  # XYZQ
    assert resp["intents_observed"] == 12

    # Per-symbol ranking — A should be at the top (5 untrusted vs
    # NVDA's 1 stale)
    by_sym = {row["symbol"]: row for row in resp["by_symbol"]}
    assert by_sym["A"]["sentinel"] == 3
    assert by_sym["A"]["stale"] == 2
    assert by_sym["A"]["untrusted_pct"] == 100.0
    assert resp["by_symbol"][0]["symbol"] == "A", (
        f"`A` should be ranked first (most untrusted); got "
        f"{[r['symbol'] for r in resp['by_symbol']]}"
    )
    assert by_sym["NVDA"]["untrusted_pct"] == 25.0  # 1/4


@pytest.mark.asyncio
async def test_lane_filter_excludes_crypto_when_equity_requested(monkeypatch):
    """`lane=equity` (default) must NOT count crypto intents.
    Operator's primary use case is equity feed-health."""
    from routes import admin_spread_quality as mod

    rows = [
        _intent("NVDA", "live", lane="equity"),
        _intent("BTC-USD", "live", lane="crypto"),
        _intent("BTC-USD", "sentinel", lane="crypto"),
    ]

    monkeypatch.setattr(mod, "db", _make_db(rows))

    resp = await mod.spread_quality_breakdown(
        hours=24, top=25, lane="equity", _user={"sub": "test"},
    )
    assert resp["totals"]["live"] == 1
    assert resp["totals"]["sentinel"] == 0  # crypto sentinel excluded
    assert resp["intents_observed"] == 1


@pytest.mark.asyncio
async def test_top_param_truncates_long_ranked_list(monkeypatch):
    """`top` must cap the by_symbol list length so the API stays
    sub-second on prod with 5k+ distinct symbols."""
    from routes import admin_spread_quality as mod

    rows = [
        _intent(f"SYM{i}", "sentinel", minutes_ago=i)
        for i in range(50)
    ]
    monkeypatch.setattr(mod, "db", _make_db(rows))

    resp = await mod.spread_quality_breakdown(
        hours=24, top=10, lane="equity", _user={"sub": "test"},
    )
    assert len(resp["by_symbol"]) == 10
    assert resp["totals"]["sentinel"] == 50  # globals unchanged


@pytest.mark.asyncio
async def test_unrecognized_quality_string_counted_as_untagged(monkeypatch):
    """A malformed `spread_quality` value (e.g. 'cosmic-ray') must
    NOT silently land in a real bucket. Treat as untagged so the
    operator can still trust the buckets."""
    from routes import admin_spread_quality as mod

    rows = [
        _intent("AAPL", "cosmic-ray"),
        _intent("AAPL", "stale"),
    ]
    monkeypatch.setattr(mod, "db", _make_db(rows))

    resp = await mod.spread_quality_breakdown(
        hours=24, top=25, lane="equity", _user={"sub": "test"},
    )
    assert resp["totals"]["stale"] == 1
    assert resp["untagged_pre_patch"] == 1


@pytest.mark.asyncio
async def test_endpoint_is_read_only(monkeypatch):
    """The endpoint MUST NOT call write methods on `shared_intents`."""
    from routes import admin_spread_quality as mod

    writes: list[str] = []

    class _Cursor:
        def __init__(self, rows): self._rows = rows
        def __aiter__(self): return self._gen()
        async def _gen(self):
            for r in self._rows: yield r

    class _ReadOnly:
        def find(self, *a, **k):
            return _Cursor([_intent("NVDA", "live")])
        def __getattr__(self, name):
            writes.append(name)
            raise AssertionError(
                f"spread-quality breakdown must be read-only — "
                f"touched {name!r}"
            )

    class _DB:
        def __getitem__(self, name): return _ReadOnly()

    monkeypatch.setattr(mod, "db", _DB())
    await mod.spread_quality_breakdown(
        hours=24, top=25, lane="equity", _user={"sub": "test"},
    )
    assert writes == [], f"unexpected writes: {writes!r}"


@pytest.mark.asyncio
async def test_query_param_bounds():
    """`hours`, `top`, and `lane` must be bounded so the response
    stays sub-second and the regex prevents wedge inputs."""
    from routes import admin_spread_quality as mod
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(mod.router)
    schema = app.openapi()
    params = schema["paths"]["/admin/spread-quality/breakdown"]["get"]["parameters"]
    by_name = {p["name"]: p for p in params}

    h = by_name["hours"]["schema"]
    assert h["minimum"] == 1 and h["maximum"] == 168

    t = by_name["top"]["schema"]
    assert t["minimum"] == 1 and t["maximum"] == 500

    lane_schema = by_name["lane"]["schema"]
    # `lane` validation lives in `pattern` — FastAPI surfaces it
    # via the schema. Just confirm the constraint exists.
    assert "pattern" in lane_schema, (
        f"`lane` param must carry a regex pattern; got {lane_schema!r}"
    )
