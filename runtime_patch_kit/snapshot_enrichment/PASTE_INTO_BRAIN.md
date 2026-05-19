# Brain — Enrich the `snapshot` block before POST `/api/ingest/intent`

## What MC is telling you

Run this from MC to see your brain's current snapshot completeness:

```bash
curl -s -H "Authorization: Bearer <admin JWT>" \
  "https://mission.risedual.ai/api/admin/intents/snapshot-completeness?hours=168" \
  | jq '.by_brain.<your_brain_id>'
```

If you see `average_completeness: 0.0000` and every field showing
`presence_rate: 0.00`, your sidecar is POSTing intents with an empty
`snapshot` block. This is the **root cause** of:

- `BLOCK_WIDE_SPREAD` on every crypto pair
- `DOCTRINE REJECT score 0.00` on every row
- `EXECUTION JUDGE → not ready` on every intent

These aren't three bugs. They're one bug — empty `snapshot` — rendered
three ways by the MC doctrine pipeline working correctly against bad
inputs.

---

## The contract

MC's `/api/ingest/intent` accepts a body shaped:

```json
{
  "stack": "alpha",                       // your brain id
  "lane": "crypto",                       // or "equity"
  "symbol": "BTC/USD",                    // exchange pair
  "action": "BUY",                        // BUY | SELL | SHORT | COVER | HOLD
  "confidence": 0.78,
  "raw_confidence": 0.81,
  "rationale": "momentum + funding clear",

  "snapshot": {
    // ←── THIS IS THE PART YOU ARE CURRENTLY OMITTING
  }
}
```

MC reads `snapshot.<field>` for every doctrine label. **Missing fields
default to sentinel values that actively poison the output** — they
don't just "lose a label," they trigger blocks. Example: missing
`spread_bps` defaults to `9999.0`, which is "infinitely wide" — MC
treats your intent as if you said "the spread is catastrophic."

---

## Required snapshot — CRYPTO

For every crypto intent (BUY / SELL / SHORT / COVER), your sidecar
MUST populate these fields. Source them from Kraken before POST:

```json
{
  "snapshot": {
    "bid": 67250.50,                       // Kraken Ticker.b[0]
    "ask": 67253.10,                       // Kraken Ticker.a[0]
    "spread_bps": 3.87,                    // (ask - bid) / bid * 10000
    "volume_24h_usd": 1_840_000_000,       // Kraken Ticker.v[1] * Ticker.p[1]
    "volatility_1h": 0.018,                // stdev of 1h returns, last 24 bars
    "trend_strength": 0.72,                // 0..1; your trend score (e.g. EMA slope, ADX-normalised)
    "exchange_liquidity_score": 0.83,      // 0..1; your depth/spread composite
    "funding_rate": 0.0001,                // perp funding (decimal, NOT bps); 0.0001 = 1 bp
    "open_interest_change_pct": 0.024,     // 24h OI change as decimal; 0.024 = +2.4%
    "liquidation_imbalance": 0.15,         // signed: + = longs liquidated, - = shorts
    "btc_regime_alignment": 0.80           // 0..1; how aligned this pair is with BTC regime
  }
}
```

**Why each field matters** (so you know what breaks if you omit it):

| Field | Default if missing | What MC does with the default |
|---|---|---|
| `spread_bps` | `9999.0` | adds `WIDE_SPREAD` label, `BLOCK_WIDE_SPREAD` Chevelle block, **score -= 0.15** |
| `volume_24h_usd` | `0.0` | skips `HIGH_24H_VOLUME` (+0.15 score not earned) |
| `volatility_1h` | `0.0` | adds `DEAD_VOL` label, **score -= 0.10** |
| `trend_strength` | `0.0` | skips `TREND_ALIGNED` (+0.15 not earned) |
| `exchange_liquidity_score` | `0.0` | skips `EXCHANGE_LIQUIDITY_OK` (+0.15 not earned) |
| `funding_rate` | `0.0` | counts as neutral; no block but score participation only |
| `open_interest_change_pct` | `0.0` | no `FUNDING_CROWDED` block but neutral score |
| `liquidation_imbalance` | `0.0` | no `LIQUIDATION_RISK` flag but neutral score |
| `btc_regime_alignment` | `0.0` | skips `BTC_REGIME_ALIGNED` (+0.10 not earned) |

Net effect of empty snapshot on a crypto intent:
- 4 negative labels (`WIDE_SPREAD`, `DEAD_VOL`, etc.)
- 0 positive labels (none of the +0.15 / +0.10 bonuses earned)
- **Score = -0.25 clamped to 0.00 = REJECT**
- Chevelle blocks: `BLOCK_WIDE_SPREAD`
- Execution judge: `execution_ready = false` (cascading from REJECT)

---

## Required snapshot — EQUITY

For every equity intent, source from Alpaca + your market-data feed:

```json
{
  "snapshot": {
    "bid": 198.42,                         // Alpaca latest_quote.bid_price
    "ask": 198.45,                         // Alpaca latest_quote.ask_price
    "spread_bps": 1.51,                    // (ask - bid) / bid * 10000
    "price": 198.43,                       // mid or last; ($1..$20 = "small account valid")
    "gap_pct": 24.5,                       // (open - prev_close) / prev_close * 100; ≥10 = gapper
    "relative_volume": 8.2,                // today's volume / 30-day avg at same time-of-day
    "has_news": true,                      // bool: catalyst present?
    "float_millions": 8.4,                 // shares outstanding (M); ≤20 = supply imbalance
    "pattern": "micro_pullback",           // one of: pullback, dip, first_pullback, micro_pullback, bull_flag, flat_top_breakout, or "" if none
    "market_regime": "strong"              // strong | green_light | momentum | weak | slow | choppy | unknown
  }
}
```

**Why each field matters:**

| Field | Default if missing | What MC does with the default |
|---|---|---|
| `spread_bps` | `999.0` | `SPREAD_TOO_WIDE` label, **score -= 0.15** |
| `price` | `0.0` | skips `SMALL_ACCOUNT_PRICE_VALID` (+0.15 not earned) |
| `gap_pct` | `0.0` | skips `GAPPER` (+0.15 not earned) |
| `relative_volume` | `0.0` | skips `HIGH_RELATIVE_VOLUME` (+0.20 not earned) — this is the biggest single contributor |
| `has_news` | `false` | adds `NO_NEWS_RISK` label (informational, not score-negative directly) |
| `float_millions` | `999999.0` | skips `LOW_FLOAT_SUPPLY_IMBALANCE` (+0.15 not earned) |
| `pattern` | `""` | no pullback label, no bonus |
| `market_regime` | `"unknown"` | no green-light bonus and no weak-market penalty |

Net effect of empty snapshot on an equity intent:
- ~0.85 worth of positive score not earned (priced/gap/RVOL/news/float/pattern)
- `SPREAD_TOO_WIDE` -0.15 penalty
- **Score = 0.00 = REJECT**

---

## Wire-in — where this code goes

This is **sidecar-side enrichment**, not a new request format. The
contract is unchanged; you just need to fill in the body you're
already sending.

Pseudocode for crypto:

```python
async def build_crypto_intent(symbol: str, action: str, conf: float) -> dict:
    # Fetch from Kraken — ONE call to /0/public/Ticker?pair=<symbol>
    ticker = await kraken.ticker(symbol)               # exists in your code
    bid = float(ticker["b"][0])
    ask = float(ticker["a"][0])
    spread_bps = (ask - bid) / bid * 10000 if bid > 0 else 9999.0

    # Volume / volatility — derive from your existing OHLC pulls
    bars_1h = await kraken.ohlc(symbol, interval=60, since=now - 24*60*60)
    volume_24h_usd = sum(b["volume"] * b["close"] for b in bars_1h)
    returns = [b["close"] / b["open"] - 1 for b in bars_1h]
    volatility_1h = stdev(returns) if len(returns) > 1 else 0.0

    # Trend / liquidity — your existing scores
    trend_strength = await your_trend_scorer(symbol)
    exchange_liquidity_score = await your_liquidity_scorer(symbol)

    # Derivatives — futures-only fields; spot pairs can return 0
    funding_rate = await your_funding_provider(symbol) or 0.0
    open_interest_change_pct = await your_oi_provider(symbol) or 0.0
    liquidation_imbalance = await your_liq_provider(symbol) or 0.0

    # BTC regime — same calc you do today for context
    btc_regime_alignment = await your_btc_regime_scorer(symbol)

    return {
        "stack": MY_BRAIN_ID,
        "lane": "crypto",
        "symbol": symbol,
        "action": action,
        "confidence": conf,
        "snapshot": {
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "volume_24h_usd": volume_24h_usd,
            "volatility_1h": volatility_1h,
            "trend_strength": trend_strength,
            "exchange_liquidity_score": exchange_liquidity_score,
            "funding_rate": funding_rate,
            "open_interest_change_pct": open_interest_change_pct,
            "liquidation_imbalance": liquidation_imbalance,
            "btc_regime_alignment": btc_regime_alignment,
        },
    }
```

---

## How to verify — without redeploying production

1. Drop the enrichment code into your sidecar
2. POST one test intent (any directional action — BUY/SELL/SHORT/COVER)
3. Hit MC:

```bash
curl -s -H "Authorization: Bearer <admin JWT>" \
  "https://mission.risedual.ai/api/admin/intents/snapshot-completeness?hours=1" \
  | jq '.by_brain.<your_brain_id>.average_completeness'
```

Expected: a value strictly greater than `0.0` (probably `1.0` if all
fields populated). The number will move toward 1.0 as more populated
intents land.

4. Now check the doctrine output on the same intent:

```bash
curl -s -H "Authorization: Bearer <admin JWT>" \
  "https://mission.risedual.ai/api/admin/decisions?brain=<your_brain_id>&limit=1" \
  | jq '.items[0].doctrine_packet | {base, seats}'
```

Expected: `base.score > 0`, `base.quality != "REJECT"`,
`seats.governor.block_reasons` no longer contains `BLOCK_WIDE_SPREAD`,
`seats.execution_judge.execution_ready` flips to `true` if
`base.score >= 0.60` AND `has_existing_intent` is true.

---

## What MC will NOT do (yet)

The natural next step is for MC to **reject** (HTTP 422) any directional
intent that lacks the required snapshot fields. That change is
**deferred until every brain has caught up.** Right now, MC observes
and reports. After every brain shows `average_completeness > 0.95`,
MC flips to strict-reject mode.

You'll see a separate paste-in doc when that flip happens.

---

## Doctrine note

Spread, score, and execution_ready are **correct reactions to your
current inputs.** Don't ask MC to "loosen Chevelle" or "lower the
score floor." The pipeline is honest; the snapshot is empty. Fill the
snapshot, the symptoms resolve in the same intent.
