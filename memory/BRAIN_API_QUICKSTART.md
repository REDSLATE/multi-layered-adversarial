# Mission Control — Brain API Quickstart (v1)

Brain-agnostic. The same endpoints, headers, and contracts work for
**Alpha, Camaro, Chevelle, and RedEye**. Substitute your brain name
wherever you see `<brain>` (lowercase: `alpha` | `camaro` | `chevelle`
| `redeye`).

For the identity / health surface, see [`BRAIN_IDENTITY_HANDOFF.md`](./BRAIN_IDENTITY_HANDOFF.md)
and [`mc_identity_v1.py`](./mc_identity_v1.py). This doc covers the
runtime/operational endpoints brains use to participate in MC.

---

## 0. Base URL and auth

| Environment | Base URL                              |
| ----------- | ------------------------------------- |
| Production  | `https://mission.risedual.ai`         |
| Preview     | `https://<preview-host>` (operator-provided) |

**Two auth schemes — never both:**

- **Runtime-token (brain path)**:
  ```
  X-Brain-Id:       <brain>
  X-Runtime-Token:  <token MC accepts for THIS brain>
  ```
- **JWT (operator path, brains don't use)**:
  ```
  Authorization: Bearer <jwt>
  ```

If you send both, MC honors the JWT path. Mismatched `X-Brain-Id` and
`X-Runtime-Token` → HTTP 401.

---

## 1. Market data (READ)

The "direct line" brains use to get derived evidence from MC's
federated market data layer.

### Single symbol

```
GET /api/admin/market-data/snapshot/{symbol}
```

**Query params**
- `tf` — timeframe (default `1Day`; also `1Hour`, `15Min`, etc.)
- `source` — `alpaca` | `polygon` | `finnhub` | `kraken` (lane-default if omitted)
- `include_news` — `true` | `false` (default `true`)

**Example**
```bash
curl "$BASE/api/admin/market-data/snapshot/NVDA?include_news=true" \
  -H "X-Brain-Id: $BRAIN" \
  -H "X-Runtime-Token: $RUNTIME_TOKEN"
```

**Response (excerpt)**
```json
{
  "symbol": "NVDA",
  "tf": "5m",
  "source": "finnhub_equity",
  "price": 142.05,
  "ohlc": { "o": 140.20, "h": 143.80, "l": 139.90, "c": 142.05, "v": 18432210 },
  "price_ok": true,
  "price_reason": null,
  "last_bar_age_sec": 47,
  "asof": "2026-05-31T20:42:18+00:00",
  "relative_volume": 1.63,
  "relative_volume_ok": true,
  "relative_volume_reason": null,
  "has_news": false,
  "has_news_ok": true,
  "served_to": "brain:<brain>",
  "doctrine": "derived_evidence_only"
}
```

**About `price`**: `price` is the **last-bar close** from MC's federation
(not a live quote). When MC has no bars for the symbol, `price` is
`null` with `price_reason: "no_bars_for_symbol"`. Brains needing a
live quote pull it directly from their broker adapter
(Alpaca/Kraken/etc) and include it in `doctrine_snapshot.price` on
the intent.

**About `spread_bps`**: **MC does NOT return `spread_bps` on this
endpoint.** Spread requires a live broker quote, which MC will not
hit on the snapshot path (`derived_evidence_only` doctrine + broker
rate-limit protection). **Brains compute and ship `spread_bps`
broker-direct in the intent's `doctrine_snapshot`.** Pulling from
Alpaca's last quote or Kraken's WS L1 is the typical pattern.

### Batch (up to 50 symbols)

```
GET /api/admin/market-data/snapshot?symbols=AAPL,NVDA,BTC/USD
```

Same query params + auth. Returns `{ "items": [ ... ], "count": N }`.

### Daily market snapshots (S&P-500-wide, frozen 3x/day)

Three frozen, point-in-time views of the full S&P-500 universe per
NYSE trading day. Useful when a brain wants to compare against the
same market state every other brain saw at that label, instead of
re-pulling per-symbol live bars.

| Label    | Captured at (US/Eastern) |
| -------- | ------------------------ |
| `open`   | 09:35                    |
| `midday` | 12:30                    |
| `close`  | 16:05                    |

Captures run automatically on every NYSE trading day; weekends and
holidays are skipped. The previous **5 trading days** are retained
(rolling). On the next `open` capture each day, rows older than the
5-day window are wiped.

**Bar sources:**
- `intraday` (5m) block → `finnhub_equity` feeder (intraday OHLCV polled every 5 minutes during market hours).
- `daily` (1d) block → `polygon` feeder (entire US equity market pulled in one grouped-daily call ~30 min after close).
- Per-block `bar_source` echoes which feeder served that row's bar; nulls land as `bar_source: null`.

```
# Which labels have been captured today?
GET /api/admin/market-data/daily-snapshots/labels

# Full universe for one label (paginated up to 1000 rows)
GET /api/admin/market-data/daily-snapshots?label=open

# Filter by symbol list
GET /api/admin/market-data/daily-snapshots?label=close&symbols=AAPL,NVDA,MSFT

# All labels for one symbol on the current market day
GET /api/admin/market-data/daily-snapshots/symbol/NVDA

# Last N (max 5) market days of snapshots for one symbol
GET /api/admin/market-data/daily-snapshots/history/NVDA?days=5
```

Same dual auth as `/snapshot/{symbol}`. Each item has **two** OHLCV
blocks, so brains can read intraday volatility AND the most-recent
daily bar from a single row:

```json
{
  "market_day": "2026-05-29",
  "label": "open",
  "symbol": "NVDA",
  "captured_at": "2026-05-31T09:29:10+00:00",
  "source": "finnhub_equity",
  "intraday_tf": "5m",
  "daily_tf": "1d",
  "intraday": {
    "tf": "5m",
    "price": 142.05,
    "ohlc": { "o": 141.80, "h": 142.30, "l": 141.55, "c": 142.05, "v": 184300 },
    "asof": "2026-05-29T13:30:00+00:00",
    "bar_source": "finnhub_equity",
    "price_ok": true, "price_reason": null,
    "relative_volume": 1.63,
    "relative_volume_ok": true, "relative_volume_reason": null,
    "basis_bars": 30, "current_v": 184300, "avg_v": 113050
  },
  "daily": {
    "tf": "1d",
    "price": 141.20,
    "ohlc": { "o": 139.10, "h": 142.80, "l": 138.40, "c": 141.20, "v": 18432210 },
    "asof": "2026-05-28T00:00:00+00:00",
    "bar_source": "finnhub_equity",
    "price_ok": true, "price_reason": null,
    "relative_volume": 1.21,
    "relative_volume_ok": true, "relative_volume_reason": null,
    "basis_bars": 30, "current_v": 18432210, "avg_v": 15234560
  }
}
```

**About the two timeframes**:
- `intraday` (5m) → the bar that just closed at capture moment.
  Open=09:30 bar, Midday=12:25 bar, Close=15:55 bar. Best for
  detecting intraday volatility / fresh RVOL spikes.
- `daily` (1d)  → the most-recent completed daily bar. On `open` and
  `midday` this is yesterday's daily; on `close` it's today's.

**Coverage gaps** — if MC has no bars for a symbol at a given
timeframe, that block lands as `price: null, price_reason:
"no_bars_for_symbol"`. Coverage is per-timeframe (intraday may
populate while daily is null and vice versa); they don't conflate.

### What's NOT here

| Want | Where | Why |
| --- | --- | --- |
| Historical OHLCV (>1 bar) | brain side / your own feeder | MC stores per-brain ingestions; no fan-out read endpoint yet |
| Live order book / level 2 | not exposed | Doctrine: brains read **derived** evidence, not raw L2 |
| Place a trade | `POST /api/intents` (Section 3) | Doctrine (c): brain proposes, MC routes |

---

## 2. OHLCV ingest (WRITE — push your bars to MC)

The feeder direction is **brain → MC**. Brains push their own OHLCV
bars; MC stores the federation copy so other surfaces (Patent J,
feature service, gate chain) can derive `spread_bps`, `relative_volume`,
`has_news`, etc.

```
POST /api/ingest/ohlcv
```

**Headers**
```
X-Brain-Id:      <brain>
X-Runtime-Token: <token>
Content-Type:    application/json
```

**Body**
```json
{
  "runtime": "<brain>",
  "symbol": "NVDA",
  "tf": "1Day",
  "bars": [
    { "ts": "2026-05-30T00:00:00Z", "o": 138.10, "h": 142.95, "l": 137.40, "c": 142.05, "v": 18432210 }
  ]
}
```

**Notes**
- `ts` MUST be ISO-8601 UTC. MC indexes on `(symbol, tf, ts)`.
- Idempotent — re-sending the same `(symbol, tf, ts)` triple upserts.
- Batch up to 500 bars per call.

---

## 3. Intent emission (the trade-request path)

**Brains do not fire orders.** Brains emit *intents*; MC runs the
12-gate chain; if all pass, MC's `broker_router` mints an execution
receipt and calls the broker. **`may_execute=True` is rejected at
the schema validator** — Doctrine (c).

```
POST /api/intents
```

**Headers**
```
X-Runtime-Token: <token>   ← must match body.stack
Content-Type:    application/json
```

**Required fields**
```json
{
  "stack": "<brain>",
  "action": "BUY",
  "symbol": "NVDA",
  "lane": "equity",
  "confidence": 0.72,
  "rationale": "post-earnings continuation; VWAP support at 138.40"
}
```

**Recommended fields (unblock more gates)**
```json
{
  "target_price": 145.20,
  "stop_price":   138.00,
  "doctrine_snapshot": {
    "price": 142.05,
    "spread_bps": 4.1,
    "relative_volume": 1.63,
    "has_news": false
  }
}
```

**Action vocabulary**
| Action | Use                                                                 |
| ------ | ------------------------------------------------------------------- |
| `BUY`  | Open long                                                           |
| `SELL` | Close long                                                          |
| `SHORT`| Open short                                                          |
| `COVER`| Close short                                                         |
| `HOLD` | Watchlist signal (passes schema; `action_routable` gate skips it)   |
| `OPEN` | Requires `direction: "long" \| "short"`; rewritten to BUY/SHORT    |
| `CLOSE`| Requires `lane`; MC discovers side+qty from broker, routes inverse |

**Honesty fields (optional but encouraged)**
Separate market judgment from execution judgment so a blocked trade
isn't silently recorded as HOLD:
```json
{
  "raw_action": "BUY",
  "raw_confidence": 0.78,
  "market_decision": "BUY",
  "execution_decision": "ALLOW",
  "display_action": "BUY"
}
```

**R:R floor (`rr_ratio_floor` gate)**
- BUY: `target_price > entry > stop_price`
- SHORT: `target_price < entry < stop_price`
- Incoherent prices (target on wrong side of entry) → HARD 422.
- 3:1 ratio enforcement is HARD from day one.
- Missing fields are SOFT today (warn-but-pass); will flip HARD when
  `RR_REQUIRE_FIELDS_HARD=true` ships. Ship the fields now.

**Schema-pinned (do NOT touch)**
| Field | Value |
| ----- | ----- |
| `may_execute` | Always `False`. MC rejects `True` with HTTP 422. |
| `requires_gate_pass` | Always `True`. MC pins on ingest. |

**Memory modulator receipt (optional)**
If your brain runs a local modulator that nudges confidence:
```json
{
  "memory_modulator": {
    "value": -0.12,
    "reasoning": "regime mismatch vs last 5 trades"
  }
}
```
- `value` MUST be in `[-0.25, +0.10]`. Out-of-bound = HARD 422 (MC
  does NOT silently clamp).
- When this receipt is present, MC TRUSTS your already-modulated
  `confidence` and does NOT recompute server-side (no double-apply).

### Read your own intents back

```
GET /api/intents?stack=<brain>&limit=50
```

Same `X-Runtime-Token` header.

---

## 4. Opinion emission (the discussion layer)

Opinions are how brains **observe and weigh in** without authority.
Includes HOLDs, vetoes, endorsements, refinements, etc. The seat
roster's "last fresh opinion" tile reads this surface.

**Executors emit intents only.** Strategist / Governor / Auditor seats
emit opinions to participate in the council. If your brain holds
executor on a lane, it does NOT need to post opinions for that lane —
its intents ARE its contribution. (See `SeatPolicy.speaks_as`.)

```
POST /api/ingest/opinion
```

**Headers**
```
X-Runtime-Token: <token>   ← must match body.runtime
Content-Type:    application/json
```

**Body**
```json
{
  "runtime": "<brain>",
  "topic": "symbol:NVDA",
  "stance": "long",
  "confidence": 0.7,
  "body": "VWAP supported, premarket high cleared, RVOL 1.6x",
  "evidence": { "rvol": 1.63, "spread_bps": 4.1 },
  "regime": "trend"
}
```

**Stance vocabulary**
`long`, `short`, `veto`, `endorse`, `question`, `observation`,
`agree`, `disagree`, `refine`, `retract`, `hypothesis`.

**Topic format**
Either `"free"` for free-form, or `"<kind>:<value>"` snake_case
identifier — e.g. `"symbol:NVDA"`, `"regime:trend"`,
`"theory:momentum_decay"`.

**Schema-pinned**
| Field | Value |
| ----- | ----- |
| `may_execute` | Always `False`. MC rejects `True` with HTTP 422. The discussion layer never carries execution authority. |

**Size caps**
- `body` ≤ 8 KB.
- `evidence` ≤ 16 KB serialized.

### Read opinions back

```
GET /api/runtime-discussion/opinions?caller=<brain>&since=<iso>
```
Brain-to-brain read path. `X-Runtime-Token` required. `since` optional
ISO cursor.

---

## 5. Seat nudges (operator → brain pings)

Operator can ping the brain currently holding a silent/missing seat on
a specific position. Brain polls for incoming nudges.

```
GET /api/runtime-discussion/seat-nudges?runtime=<brain>&since=<iso>
```

**Headers**
```
X-Runtime-Token: <token>
```

**Query params**
- `runtime` — your brain name (required)
- `since` — ISO timestamp cursor; only nudges with `ts > since` are returned

**Response**
```json
{
  "runtime": "<brain>",
  "since": "2026-05-31T20:00:00Z",
  "count": 1,
  "items": [
    {
      "nudge_id": "...",
      "position_id": "...",
      "symbol": "NVDA",
      "seat": "governor",
      "brain": "<brain>",
      "sent_by_email": "admin@risedual.io",
      "message": "please stance on AAPL",
      "ts": "2026-05-31T20:14:32Z",
      "authority": "advisory_observability_only"
    }
  ]
}
```

**Suggested poll cadence**: every 60s. The nudge surface is `advisory_observability_only` — MC never pushes; you poll. No retry, no escalation; the operator owns the cadence.

---

## 6. Sidecar check-in (lifecycle ping)

If your brain ships the `mc_identity_v1.py` drop-in (recommended —
see the identity handoff doc), this fires automatically every 300s.

```
POST /api/admin/runtime/sidecar-checkin/<brain>
```

**Headers**
```
X-Runtime-Token: <token>
Content-Type:    application/json
```

**Body** (same `identity` block your `GET /status` serves)
```json
{
  "identity": {
    "app_name": "<brain>",
    "env_name": "prod",
    "git_sha": "abc123",
    "broker_mode": "paper",
    "sidecar_version": "2.4.1",
    "mc_url_set": true,
    "ingest_token_set": true,
    "mc_base_url_set": true,
    "heartbeat_token_set": true,
    "checkin_worker_eligible": true
  }
}
```

MC stamps `last_checkin_at` and surfaces it on the operator's
diagnostics tile.

---

## 7. Common errors and what they mean

| Status | When it fires | What to do |
| ------ | ------------- | ---------- |
| 401 | Bad/missing `X-Runtime-Token`, or token doesn't match `body.runtime`/`body.stack` | Verify the token MC issued for your brain |
| 422 `may_execute=True is forbidden` | Brain set `may_execute: true` on intent or opinion | Doctrine (c). Always set `False` — MC pins it server-side anyway |
| 422 `memory_modulator.value out of bounds` | Brain shipped modulator outside `[-0.25, +0.10]` | Clamp on YOUR side before posting; MC will NOT silently clamp |
| 422 `target_price/stop_price coherence` | BUY with `target < entry` or SHORT with `target > entry` | Broken intent — recompute with correct sign |
| 429 `nudge_cooldown` | Nudge endpoint only; same (position, seat) within 30min | Honor `retry_after_seconds` in body |
| Intent stamped `gate_state: dry_run_blocked` | MC's 12-gate chain rejected the intent | Check `GET /api/admin/execution/last-block-reason?stack=<brain>` for the failing gate + reason |

---

## 8. End-to-end flow (the canonical path)

```
1. Brain reads market data
   GET /api/admin/market-data/snapshot/{symbol}

2. Brain decides (locally) → emits intent
   POST /api/intents
   { stack, action, symbol, lane, confidence, rationale,
     target_price, stop_price, doctrine_snapshot }

3. MC runs 12-gate chain (incl. R:R, spread, seat, exposure caps)

4. If all gates pass AND lane execution toggle is enabled:
     MC's broker_router mints an execution receipt
     adapter.submit_market_order(mc_receipt=<receipt>)
     gate_state → "passed"; broker fills

5. If a gate fails:
     gate_state → "dry_run_blocked"
     Auditable via /api/admin/execution/last-block-reason

6. Brain participates in the council (separately):
   POST /api/ingest/opinion  (stances, vetoes, endorsements, HOLDs)

7. Brain polls for operator nudges:
   GET /api/runtime-discussion/seat-nudges?runtime=<brain>
```

---

## 9. What's coming (heads-up for forward-compat)

- **`rr_ratio_floor` HARD enforcement**: Phase B will flip missing
  `target_price` / `stop_price` from warn to block. Ship them now.
- **Doctrine quality threshold**: MC may lower Patent J threshold,
  letting more intents auto-route. Doctrine label is informational
  today on the intent ingest path; that may change.
- **Cross-brain federation bridge** (Phase 3): brains' local
  Shellys will be visible across MC. Don't hardcode your brain's
  identity into evidence payloads.

---

## 10. Verifying your wire

Three sanity checks any brain can run against MC right after deploy:

```bash
# (1) Identity surface
curl -H "X-Brain-Id: $BRAIN" -H "X-Runtime-Token: $TOKEN" \
  "$BASE/api/admin/runtime/$BRAIN/status" | jq .payload.identity

# (2) Market data
curl -H "X-Brain-Id: $BRAIN" -H "X-Runtime-Token: $TOKEN" \
  "$BASE/api/admin/market-data/snapshot/NVDA" | jq .price

# (3) Emit a HOLD intent (always safe — non-routable)
curl -X POST "$BASE/api/intents" \
  -H "X-Runtime-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"stack\":\"$BRAIN\",\"action\":\"HOLD\",\"symbol\":\"NVDA\",\"lane\":\"equity\",\"confidence\":0.5,\"rationale\":\"smoke test\"}" \
  | jq .intent_id
```

If all three return non-null payloads, your wire is good.

---

*Doc version: v1 (2026-05-31). MC owner: Mission Control. Contract pinning: see `backend/tests/test_intent_schema_doctrine.py` for the IntentIn invariants this doc describes.*
