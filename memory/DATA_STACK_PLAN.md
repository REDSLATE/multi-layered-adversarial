# Mission Control — Market Data Stack Plan

**Status**: PLAN ONLY (not yet implemented)
**Drafted**: 2026-05-27 (pass #12 follow-up)
**Owner**: pending — next agent or operator-led implementation

---

## 0. Executive summary

Diversified, fail-soft market-data architecture targeting **small-cap NASDAQ + crypto + alternative data**. Avoids single-provider lock-in (Polygon's price creep + reliability concerns). Plugs into the existing `FEEDERS` config in `shared/technicals.py` so adding a new provider stays a 2-line change.

**Layers:**

| Layer | Providers | Cost | Status |
|---|---|---|---|
| **Primary OHLCV** | Finnhub | $0 → $50/mo Standard | New |
| **Secondary OHLCV** | Alpha Vantage, yfinance | $0 → $50/mo | New (yfinance backfill-only) |
| **Crypto** | Kraken Pro, Binance | $0 (broker keys) | Kraken wired; Binance new |
| **Alt data** | Quiver, SEC EDGAR, FRED | $0 → $50/mo (Quiver) | All new |
| **Options/Flow** | Tradier sandbox → Intrinio | $0 → $100+/mo | Phase 2 deferral |

**Doctrine pin**: Market data is evidence. Brains read it; brains weight it; seat holder acts. MC verifies feed integrity (auth, schema, freshness) but **never evaluates trade quality based on the data**. Adding a new provider must not introduce any execution-authority path.

---

## 1. Architecture overview

### 1a. Existing infrastructure (already wired)

- `shared/technicals.py` — feeder ingest + technical-snapshot read
- `shared_ohlcv_bars` collection — append-only, keyed on `(source, symbol, tf, ts)`
- `shared_indicator_snapshots` — derived deterministic indicators
- `shared_pattern_snapshots` — pattern detector output (pass #10)
- `FEEDERS` dict — feeder identity → env-var token mapping
- `/api/ingest/ohlcv` + `/api/ingest/ohlcv/batch` — feeder-token-auth POST endpoints
- `/api/shared/technical/{symbol}` + `/api/runtime-discussion/technical/{symbol}` — read endpoints
- `/api/admin/patterns/scan` — Pattern Watch ranking (pass #12)

### 1b. New collections to add

```
symbol_metadata             — per-symbol facts (float, market cap, sector, listing venue)
alt_data_filings            — SEC EDGAR filing references + cached extracts
alt_data_macro              — FRED series cache (CPI, fed funds, unemployment, etc.)
alt_data_alpha_signals      — Quiver: congressional trades, insider Form 4, WSB sentiment
patterns_universe           — operator-managed watchlist (which symbols MC actively scans)
feeder_health_audit         — per-feeder rate-limit + error tracking (parallels runtime_token_audit)
```

### 1c. New config additions

**`shared/technicals.py`** — extend `FEEDERS`:

```python
FEEDERS: dict[str, str] = {
    "kraken_pro":       "KRAKEN_FEEDER_TOKEN",
    "thinkorswim":      "TOS_FEEDER_TOKEN",
    "finnhub_equity":   "FINNHUB_FEEDER_TOKEN",       # ← new (Finnhub-pushed bars)
    "yfinance_backfill": "YFINANCE_FEEDER_TOKEN",     # ← new (historical backfill only)
    "alpha_vantage":    "ALPHA_VANTAGE_FEEDER_TOKEN", # ← new (fallback)
    "binance":          "BINANCE_FEEDER_TOKEN",       # ← new (crypto redundancy)
    "manual":           "MANUAL_FEEDER_TOKEN",
}
```

**New `shared/alt_data.py`** module — non-OHLCV evidence layer:

```python
ALT_DATA_SOURCES: dict[str, str | None] = {
    "sec_edgar":     None,                  # public, no token required
    "fred":          "FRED_API_KEY",
    "quiver":        "QUIVER_API_KEY",
    "alpha_vantage_fundamentals": "ALPHA_VANTAGE_API_KEY",
}
```

### 1d. New `.env` variables required

```bash
# Equity data providers — pick which ones you commit to
FINNHUB_API_KEY=                # finnhub.io — free signup
FINNHUB_FEEDER_TOKEN=           # internal token for /api/ingest/ohlcv auth
ALPHA_VANTAGE_API_KEY=          # alphavantage.co — free signup
ALPHA_VANTAGE_FEEDER_TOKEN=
YFINANCE_FEEDER_TOKEN=          # no API key needed; just feeder-side token

# Crypto redundancy
BINANCE_FEEDER_TOKEN=
BINANCE_API_KEY=                # public bars are unauthed, but rate-limited higher with a key
BINANCE_API_SECRET=

# Alt-data
FRED_API_KEY=                   # fred.stlouisfed.org — free
QUIVER_API_KEY=                 # api.quiverquant.com — paid

# Options (Phase 2)
TRADIER_SANDBOX_TOKEN=          # tradier.com — free sandbox
TRADIER_API_TOKEN=              # paid live
INTRINIO_API_KEY=               # intrinio.com — paid

# Rate-limit alarms (sane defaults if not set)
FEEDER_RATE_LIMIT_WARN_PCT=80   # warn at 80% of provider's published rate limit
```

**Protected vars to keep untouched** (per `/app/memory/PRD.md` rules):
- `MONGO_URL`, `DB_NAME`, `REACT_APP_BACKEND_URL`, `KRAKEN_FEEDER_TOKEN`, `TOS_FEEDER_TOKEN`

---

## 2. Per-provider integration playbook

> ⚠️ **Before implementing any provider, call `integration_playbook_expert_v2`** for the verified per-provider patterns (auth, pagination, rate-limit handling, SDK choices). What follows below is architectural sketch only.

### 2a. Finnhub (PRIMARY equity)

**Why**: best small-cap coverage at this price point. Has `/stock/profile2` for float (powers `small_cap_qualified` flag) and WebSocket on paid tier.

**Endpoints needed**:
- `GET /api/v1/stock/candle?symbol=HOTH&resolution=5&from=...&to=...` — OHLCV bars
- `GET /api/v1/stock/profile2?symbol=HOTH` — company profile incl. `shareOutstanding`
- `GET /api/v1/quote?symbol=HOTH` — real-time quote (last price, bid/ask)
- `wss://ws.finnhub.io?token=...` — WebSocket streaming (Standard+ only)

**Auth**: `?token=<FINNHUB_API_KEY>` query param OR `X-Finnhub-Token` header.

**Rate limits**:
- Free: 60 calls/min, 30 calls/sec
- Standard ($50/mo): 300 calls/min
- Premium: WebSocket streaming + 600 calls/min

**Code skeleton** (`shared/feeders/finnhub_equity.py` — new):

```python
"""Finnhub equity feeder — primary OHLCV source for US equities.

Runs as a background task (separate process or in-app worker, TBD).
Polls candles for symbols in `patterns_universe` and POSTs to
/api/ingest/ohlcv/batch using FINNHUB_FEEDER_TOKEN.

Also populates symbol_metadata with float_shares_millions from
/stock/profile2 — refreshed weekly per symbol.
"""

POLL_INTERVAL_SEC = 60      # for active watchlist
BATCH_SIZE = 50             # POST /api/ingest/ohlcv/batch limit
SYMBOL_REFRESH_DAYS = 7     # how often to re-pull profile2 for float updates

async def poll_finnhub_candles(symbols: list[str], tf: str = "5") -> None:
    """For each symbol, fetch the last N candles via /stock/candle and
    POST to MC's ingest endpoint. Tracks rate-limit headers (X-Ratelimit-*)
    and writes to feeder_health_audit on near-limit / 429 responses."""
    ...

async def refresh_symbol_metadata(symbol: str) -> None:
    """Pull /stock/profile2 and upsert to symbol_metadata. Powers
    pattern detector's small_cap_qualified flag automatically."""
    profile = await _http_get(f"/stock/profile2?symbol={symbol}", ...)
    await db["symbol_metadata"].update_one(
        {"symbol": symbol},
        {"$set": {
            "symbol": symbol,
            "float_shares_millions": profile.get("shareOutstanding"),
            "market_cap_millions": profile.get("marketCapitalization"),
            "sector": profile.get("finnhubIndustry"),
            "exchange": profile.get("exchange"),
            "country": profile.get("country"),
            "source": "finnhub",
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
```

**Failure mode**: 429 rate-limit → exponential backoff + flip to Alpha Vantage fallback for affected symbols.

---

### 2b. Alpha Vantage (SECONDARY equity + fundamentals fallback)

**Why**: best free fundamentals when batched. Free tier is 25 calls/day (brutal for live OHLCV), so use it as:
- **Fundamentals backup** when Finnhub `/stock/profile2` is rate-limited or unavailable
- **Daily-bar backfill** for new symbols (one call per symbol, fits in free tier)

**Endpoints**:
- `GET /query?function=TIME_SERIES_DAILY&symbol=HOTH&apikey=...` — daily OHLCV
- `GET /query?function=OVERVIEW&symbol=HOTH&apikey=...` — fundamentals (float, market cap, P/E, sector)
- `GET /query?function=NEWS_SENTIMENT&tickers=HOTH&apikey=...` — news (free tier limited)

**Auth**: `&apikey=<ALPHA_VANTAGE_API_KEY>` query param.

**Rate limits**:
- Free: 25 calls/day, 5 calls/min
- Premium $50/mo: 75 calls/min
- Premium $250/mo: 1200 calls/min

**Use pattern**: ONLY hit when Finnhub fails. Cache fundamentals in `symbol_metadata` for 7 days.

---

### 2c. yfinance (SECONDARY — backfill only)

**Why**: free, unlimited (rate-limited silently by Yahoo), huge symbol coverage. Use for **historical bar backfill** when adding a new symbol to the universe.

**Critical TOS note**: Yahoo's ToS prohibits commercial redistribution. For solo-operator system this is gray area; for any multi-user product, swap to a paid feed before launch.

**Endpoints**: via the `yfinance` Python package — no HTTP API to call directly. Wraps Yahoo's unofficial chart API.

```python
# Example backfill call
import yfinance as yf
ticker = yf.Ticker("HOTH")
hist = ticker.history(period="6mo", interval="1d")  # DataFrame
# Convert + POST to /api/ingest/ohlcv/batch via yfinance_backfill feeder token
```

**Endpoint to build**: `POST /api/admin/feeders/yfinance-backfill?symbol=HOTH&days=180&tf=1d` — operator-triggered backfill, not background polling.

**Failure mode**: silently breaks every few months when Yahoo changes their endpoint. Pin the `yfinance` library version + add a tripwire that fetches a known symbol and asserts ≥10 bars returned.

---

### 2d. Kraken Pro (CRYPTO — already wired)

Already in `FEEDERS` as `kraken_pro` with `KRAKEN_FEEDER_TOKEN`. No new work needed unless you want to add new pairs.

---

### 2e. Binance (CRYPTO redundancy)

**Why**: cross-check Kraken's prices to catch feeder drift (spread enrichment improvement). Also gives you futures + options later.

**US-Restriction warning**: Binance.com is blocked for US IPs. If you're US-based, use `Binance.US` (smaller universe but legal) OR run the feeder from a non-US server.

**Endpoints**:
- `GET /api/v3/klines?symbol=BTCUSDT&interval=5m&limit=200` — OHLCV (unauthed, rate-limited)
- `GET /api/v3/ticker/bookTicker?symbol=BTCUSDT` — bid/ask (spread enrichment)
- `wss://stream.binance.com:9443/ws` — WebSocket streaming (free)

**Rate limits**: 1200 weight/min unauthenticated; 6000/min with API key. Bars cost weight=1 each.

---

### 2f. SEC EDGAR (ALT — free, official)

**Why**: source-of-truth for 10-K, 10-Q, 13F, Form 4 (insider transactions). Form 4 in particular is a strong day-trader signal — insiders buying/selling.

**Endpoints**:
- `GET /submissions/CIK<10-digit-cik>.json` — list of recent filings per company
- `GET /Archives/edgar/data/<cik>/<accession>/<filename>.htm` — raw filing
- `GET /cgi-bin/browse-edgar?action=getcompany&CIK=<ticker>&type=4` — Form 4 search

**Auth**: requires a `User-Agent` header with your name + email (SEC's politeness rule). NO API key needed.

**Rate limits**: 10 requests/sec per IP. Bulk filings via FTP for large pulls.

**Recommended Python lib**: `sec-edgar-api` or `edgar-tools` — handles the user-agent + parsing.

**Use pattern**: poll Form 4 for symbols in `patterns_universe` daily. Insider buy/sell with size >$50k → write to `alt_data_alpha_signals` with `kind="insider_form4"`.

---

### 2g. FRED (ALT — macro)

**Why**: regime classification. Brains can use macro state (CPI YoY, fed funds, unemployment trend) as a feature in their decision models.

**Endpoints**:
- `GET /fred/series/observations?series_id=CPIAUCNS&api_key=...&file_type=json` — series data

**Auth**: `&api_key=<FRED_API_KEY>` query param. Free signup.

**Rate limits**: 120 requests/min — very generous.

**Series to cache**:
- `CPIAUCNS` — CPI
- `UNRATE` — unemployment
- `FEDFUNDS` — federal funds rate
- `DGS10` — 10-year Treasury yield
- `T10Y2Y` — yield curve spread (recession signal)

**Refresh cadence**: daily (FRED updates monthly anyway).

---

### 2h. Quiver Quant (ALT — alternative signals)

**Why**: congressional trades, lobbying disclosures, WSB sentiment, government contracts. Each is a documented alpha source.

**Endpoints**:
- `GET /beta/live/congresstrading` — recent congressional stock trades
- `GET /beta/historical/wallstreetbets/<symbol>` — WSB mention count + sentiment
- `GET /beta/live/governmentcontracts` — federal contract awards

**Auth**: `Authorization: Bearer <QUIVER_API_KEY>` header.

**Rate limits**: vary by tier; check current docs.

**Pricing**: $10-50/mo depending on which endpoints you need.

**Doctrine note**: alt-data is descriptive evidence ONLY. Brains may consume; MC stores; never gates trades on alt-data alone.

---

### 2i. Tradier / Intrinio (OPTIONS — Phase 2 deferral)

**Tradier Sandbox** (free with signup):
- `GET /v1/markets/options/chains?symbol=HOTH&expiration=2026-06-19` — full chain
- `GET /v1/markets/options/strikes?symbol=HOTH&expiration=...` — strike list
- Quote-level streaming via REST polling or HTTP streaming

**Intrinio** ($50+/mo): better backfill + broader symbol coverage; enterprise SLA.

**Defer until equity pipeline is proven**. Options add real complexity (Greeks, IV calc, expiry handling).

---

## 3. Phased rollout

### Phase 1 — Free layer (this week if approved)

**Goal**: Pattern Watch tile populating with real symbols within 1 day of deploy.

1. **Call `integration_playbook_expert_v2`** for Finnhub + Alpha Vantage + yfinance + SEC EDGAR + FRED playbooks
2. Get `FINNHUB_API_KEY` from operator (free signup at finnhub.io)
3. Get `FRED_API_KEY` from operator (free signup at fred.stlouisfed.org)
4. Build `shared/feeders/finnhub_equity.py`:
   - Polling worker for OHLCV → `/api/ingest/ohlcv/batch`
   - Weekly profile2 refresh → `symbol_metadata` collection
5. Build `POST /api/admin/feeders/yfinance-backfill` endpoint for historical bars
6. Build `shared/alt_data/sec_edgar.py` + `shared/alt_data/fred.py`
7. Build `POST /api/admin/patterns/universe` for operator-managed watchlist
8. Modify `shared/patterns/base_breakout.py` — pull `float_shares_millions` from `symbol_metadata` automatically (instead of requiring query param)
9. Tripwires:
   - Each feeder schema test
   - Rate-limit handling (429 → backoff + audit row)
   - Symbol metadata population
   - Universe CRUD
10. Operator approval before deploy

**Estimated effort**: 4-6 hours of focused work.

### Phase 2 — Commit ($50-100/mo)

1. Upgrade Finnhub to Standard ($50/mo) — unlocks 300 calls/min + WebSocket
2. Add Binance feeder (free) for crypto redundancy
3. Sign up Quiver ($10-50/mo) — wire `shared/alt_data/quiver.py`
4. Add `congressional_trades` + `insider_form4` + `wsb_sentiment` tiles to Overview

### Phase 3 — Pro layer (Phase 2 deferral)

1. Alpha Vantage Premium ($50/mo) — high-frequency fundamentals + technicals fallback
2. Tradier sandbox wiring for options chains
3. Intrinio if going fully pro

---

## 4. Failover strategy

**Per-symbol routing logic** (in a new `shared/feeders/router.py`):

```python
async def fetch_bars(symbol: str, tf: str, n: int) -> list[Bar]:
    """Try primary, fall through to secondaries on failure."""
    for source in ("finnhub_equity", "alpha_vantage", "yfinance_backfill"):
        try:
            bars = await _fetch_from(source, symbol, tf, n)
            if bars:
                return bars
        except RateLimitError:
            await audit_rate_limit(source, symbol)
            continue
        except FeederUnavailable:
            await audit_unavailable(source, symbol)
            continue
    raise NoFeederAvailable(symbol)
```

**Daily health roll-up endpoint**: `GET /api/admin/feeders/health-audit` showing per-feeder error rates, rate-limit hits, and last-success age.

---

## 5. Doctrine pins

These must remain invariant across all providers:

1. **Market data is evidence.** Brains read it; brains weight it; seat holder acts. MC never modifies trade authority based on which data source returned a bar.

2. **No feeder can carry execution authority.** The `OHLCVBarIn` schema must continue to reject any `may_execute` field.

3. **Alt-data signals are descriptive.** Form 4, congressional trades, WSB sentiment, FRED macro — all are *features* for brain decision models. Brains decide; MC stores.

4. **Rate-limit audits never block ingest.** A 429 is logged + fallback engaged. The MC pipeline degrades gracefully.

5. **`symbol_metadata.float_shares_millions` is the single source of truth** for `small_cap_qualified`. Brains should not pass that flag manually anymore once Phase 1 ships.

6. **TOS compliance.** yfinance is for solo-operator backfill ONLY. If MC ever becomes multi-user, swap to a paid feed before launch.

---

## 6. Tripwires the implementer must add

| Test | What it locks |
|---|---|
| `test_finnhub_feeder_schema_pinned` | Bars POST'd must match `OHLCVBarIn` shape |
| `test_finnhub_rate_limit_audit` | 429 response writes one row to `feeder_health_audit` |
| `test_yfinance_backfill_endpoint_authed` | `/api/admin/feeders/yfinance-backfill` requires JWT |
| `test_yfinance_breakage_alarm` | Backfill of known symbol (e.g. AAPL) returns ≥10 bars |
| `test_alpha_vantage_fallback_path` | When `finnhub_equity` returns 429, `alpha_vantage` is tried next |
| `test_symbol_metadata_populates_small_cap_flag` | After `refresh_symbol_metadata("HOTH")`, pattern detector returns `small_cap_qualified` without query-param help |
| `test_patterns_universe_crud` | Operator can add/remove symbols from watchlist |
| `test_sec_edgar_form4_parse` | Known Form 4 filing parses to expected structure |
| `test_fred_series_cache` | Series fetched once is cached for ≥24h |
| `test_doctrine_no_execution_authority_in_alt_data` | `alt_data_*` collections never carry `may_execute` keys |
| `test_feeder_health_audit_aggregator` | `/admin/feeders/health-audit` returns per-feeder error counts |

---

## 7. UI surfaces to add (Phase 1)

- **Feeders strip** on Overview — already exists (`shared/technical/feeders`). Add Finnhub + yfinance + Alpha Vantage to the rendered list.
- **Universe management tile** — small operator-facing list with add/remove buttons → `POST /api/admin/patterns/universe`.
- **Symbol metadata badges** — when a symbol appears anywhere in MC (intent, opinion, position), show `SMALL CAP` / `MID CAP` / `LARGE CAP` badge derived from `symbol_metadata.market_cap_millions`.

---

## 8. Decision log to maintain

When implementing Phase 1, document in CHANGELOG.md:

1. Which provider was chosen for primary OHLCV (Finnhub) + why
2. Which symbols are in the initial `patterns_universe` seed list
3. Polling cadence chosen (1-min vs 5-min) + reasoning
4. Whether the operator opted for free-tier Finnhub vs Standard from day 1
5. Any TOS questions about yfinance left open

---

## 9. Open questions for the next implementer

1. **Where does the feeder worker run?** Options:
   - Inside the MC backend process (asyncio task on startup)
   - Separate sidecar pod (cleaner, matches the brain-sidecar pattern)
   - External cron worker
   Recommended: **separate sidecar pod** — matches existing doctrine and isolates rate-limit issues from MC's main event loop.

2. **WebSocket vs polling for Finnhub Standard?** Polling is simpler; WebSocket gives sub-second latency. Pattern detector doesn't need sub-second — recommend **5-min polling** for Phase 1, upgrade to WebSocket only if a brain specifically requires real-time.

3. **Symbol seeding strategy?**
   - Manual seed list (operator picks)
   - Daily auto-pull of "top 100 small-caps by volume" from Finnhub `/stock/symbol`
   - Hybrid: 20 manual + 80 auto-discovered
   Recommended: **start manual** with 10-20 symbols, expand to hybrid in Phase 2.

4. **Crypto pair list?** Currently only Kraken (BTC/USD, ETH/USD). Binance opens up alt-coins (DOGE, SHIB, AVAX, etc.) — but only if you want those lanes. Confirm with operator before adding pairs to the universe.

5. **Tradier sandbox now or later?** Options data is rich evidence but adds complexity. Defer to Phase 3.

---

## 10. References

- Finnhub docs: https://finnhub.io/docs/api
- Alpha Vantage docs: https://www.alphavantage.co/documentation/
- yfinance GitHub: https://github.com/ranaroussi/yfinance
- Binance API docs: https://binance-docs.github.io/apidocs/spot/en/
- SEC EDGAR API: https://www.sec.gov/edgar/sec-api-documentation
- FRED API: https://fred.stlouisfed.org/docs/api/fred/
- Quiver Quant: https://api.quiverquant.com/docs/
- Tradier docs: https://documentation.tradier.com/
- Intrinio: https://intrinio.com/data-marketplace

**Mission Control internal references**:
- `/app/backend/shared/technicals.py` — feeder ingest pattern
- `/app/backend/shared/patterns/base_breakout.py` — pattern detector that consumes this data
- `/app/memory/CHANGELOG.md` — pass history
- `/app/memory/PRD.md` — original problem statement + doctrine
