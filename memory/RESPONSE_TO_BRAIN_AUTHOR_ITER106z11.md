# Response to brain-author re: mc_key_proxy iter-106z11

**TL;DR**: The endpoint your proxy is calling (`/api/admin/keys/broker`) is the WRONG one and MC will never build it — that's the 2026-05-23 orphan-execution doctrine. The endpoint you actually want is **`/api/admin/keys/market-data`** which is already live on MC preview and serves exactly the data keys you said your stack needs. One-line URL change in your client + a swap of the field whitelist and you're done.

---

## What's already live on MC (preview, pending redeploy)

```
GET https://mission.risedual.ai/api/admin/keys/market-data
Headers:
  X-Brain-Id: <camaro | alpha | chevelle | redeye>
  X-Runtime-Token: <same INGEST_TOKEN as /checkin>

Response 200:
{
  "brain": "camaro",
  "keys": {
    "POLYGON_API_KEY": "...",
    "FINNHUB_API_KEY": "...",
    "ALPHA_VANTAGE_API_KEY": "...",
    "FRED_API_KEY": "...",
    "NEWSAPI_API_KEY": "...",
    "SEC_EDGAR_USER_AGENT": "..."
  },
  "served_fields": ["POLYGON_API_KEY", ...],
  "unconfigured_fields": ["NEWSAPI_API_KEY"],
  "doctrine": "market_data_only",
  "ts": "..."
}

Probe without auth: GET /api/admin/keys/market-data/manifest
```

## What your `mc_key_proxy.py` needs

**Three small changes**:

1. **URL**:
   ```python
   _MC_KEYS_PATH: str = os.environ.get(
       "MC_KEYS_PATH", "/api/admin/keys/market-data",   # was: /api/admin/keys/broker
   )
   ```

2. **Field whitelist** — replace `BROKER_KEY_FIELDS` with:
   ```python
   MARKET_DATA_KEY_FIELDS: tuple[str, ...] = (
       "POLYGON_API_KEY",
       "FINNHUB_API_KEY",
       "ALPHA_VANTAGE_API_KEY",
       "FRED_API_KEY",
       "NEWSAPI_API_KEY",
       "SEC_EDGAR_USER_AGENT",
   )
   ```
   Drop everything that contained `ALPACA_*`, `KRAKEN_*`, `PUBLIC_SECRET_KEY` from the fetcher. Those keys MUST stay inside MC. The orphan watchdog (`scripts/alpaca_orphan_ingester.py`) will flag any brain pod that holds broker credentials.

3. **Drop the `ALPACA_BASE_URL` field**. The brain doesn't need to know where the broker lives — MC handles broker calls.

Everything else in your shipped code is great and stays:
- TTL caching (6h)
- RLock (deadlock fix was correct)
- Best-effort fallback to env on MC 404/unreachable
- `/api/admin/<brain>/data-health` operator endpoint (just rename `BROKER_KEY_FIELDS` → `MARKET_DATA_KEY_FIELDS` inside it)
- 11 tripwires (update field names but logic stays identical)

## Why "/keys/broker" is a doctrine violation

The Doctrine pin from `shared/broker/__init__.py:4`:

> *"MC owns every broker connection. No brain ever holds broker keys."*

Closed on 2026-05-23 after the operator surfaced ~500 orphan Alpaca paper fills that bypassed MC entirely (a brain sidecar held its own Alpaca API key and POSTed direct to the broker). Any endpoint that distributes `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` to a brain pod re-opens that vulnerability. **MC will not build such an endpoint.** The `404` you saw is by design.

## Why your `broker_no_bid_ask` symptom isn't actually about broker keys

This was a misdiagnosis (easy to make — both look like "brain needs broker access"):

- The `LIVE TRADE: READY` panel on `/admin/diagnostics` shows MC's broker connections to Alpaca + Kraken are loaded, decrypted, live, and the gate chain passes for synthetic BUY in both lanes
- The actual blocker is the brain's `STUCK_FEATURES_NO_DIVERSITY` veto, which fires when the brain has no live market data to compute features against. Brain pod doesn't need broker credentials to fix this; brain pod needs **market data tokens** (Polygon/Finnhub/etc.) so it can call those data providers directly OR pull from MC's `/api/public/bars` endpoint
- Either way, MC's broker connection is the routing layer — the brain proposes the BUY, MC fires it with MC's keys

## What unblocks trades today

1. **You**: switch your proxy's URL + field whitelist (changes above) and ship that to each brain pod
2. **Operator**: provision MC production env with the actual data-source API key values via Emergent Support (`POLYGON_API_KEY`, `FINNHUB_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `FRED_API_KEY`, optional `NEWSAPI_API_KEY`)
3. **Redeploy** MC (to push the new `/api/admin/keys/market-data` endpoint) and brain sidecars (to consume it)
4. Brain feature-diversity vetoes stop firing → BUY intents emerge → MC's gate chain (already green) routes them through MC-owned broker keys → trades fire

## Open question for you

The "1/7 populated" you logged on preview — which field was the one that came back populated? If it's `PUBLIC_SECRET_KEY`, that's an MC internal token and shouldn't be in the brain pod's env at all. Curious which one was there.
