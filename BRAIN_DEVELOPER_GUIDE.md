# Brain Developer Guide

Single source of truth for any brain pod (Alpha, Camaro, Chevelle, REDEYE, or any future
sidecar) integrating with Mission Control (MC).

**This file lives in the MC repo. Treat any deviation from this spec as a brain-side bug.**

Last updated: 2026-02-18 (Ladder Doctrine Phase 3 shipped).

---

## 1. Roles & responsibilities

| Side | Owns | Does NOT own |
|---|---|---|
| **Brain pod** | Market opinion, conviction, direction, size suggestion, market snapshot | Receipt creation, gate logic, broker calls, ladder state, expectancy stats |
| **Mission Control** | Gate chain, broker routing, observation receipts, resolver, ladder counter, audit | Brain conviction, market hypothesis, sizing logic |

The two sides are intentionally separated. **A brain never POSTs receipts, fills,
expectancies, ladder transitions, or graded outcomes.** MC creates and owns all of those —
otherwise the brain could fabricate evidence for its own promotion. The brain's only job
is to honestly emit an opinion.

---

## 2. URLs

```
PRODUCTION:  https://mission.risedual.ai
PREVIEW:     <whatever your preview emergentagent.com URL is>
```

Your `mc_url` in the runtime stamp must match the URL you actually POST to. MC's validator
strictly compares against the production URL prefix when deciding `verdict=prod`.

---

## 3. Authentication

Every POST or non-public GET against MC requires one of:

* **Operator JWT** — for admin endpoints, comes from `/api/auth/login`. Brains don't use this.
* **`X-Runtime-Token` header** — per-brain, in MC's `.env` as `<BRAIN>_INGEST_TOKEN`
  (`ALPHA_INGEST_TOKEN`, `CAMARO_INGEST_TOKEN`, etc.).

Brains exclusively use the runtime token. **Don't name it anything else** (no
`MONOREPO_INGEST_TOKEN`, no `BRAIN_TOKEN`, no `API_KEY`). The header value is the secret;
the header name is fixed.

```
POST https://mission.risedual.ai/api/intents
Headers:
  Content-Type: application/json
  X-Runtime-Token: <YOUR_BRAIN_INGEST_TOKEN>
```

Don't share tokens between brains. Don't reuse preview tokens against prod.

---

## 4. The ONLY ingest endpoint you need: `POST /api/intents`

This is the entire integration. Everything else flows from this.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `stack` | string | `"alpha" \| "camaro" \| "chevelle" \| "redeye"`. Must match your token. |
| `action` | string | `"BUY" \| "SELL" \| "SHORT" \| "COVER" \| "HOLD"` |
| `symbol` | string | Equity: `"AAPL"`. Crypto: `"BTC/USD"`. Use the canonical form your broker uses. |
| `lane` | string | `"equity"` or `"crypto"` |
| `confidence` | float | Unit-scale `[0.0, 1.0]`. Brain's calibrated conviction (not raw probability). |
| `rationale` | string | Free-form, up to 4000 chars. Why you emitted this. Captured for audit. |

### The contract field — `doctrine_snapshot`

```json
"doctrine_snapshot": {
  "spread_bps":         8,           // REQUIRED — gate 7 fail-closes without this
  "price":              580.40,      // anchor for resolver; required for graded learning
  "volume":             12500000,
  "relative_volume":    2.3,
  "gap_pct":            1.5,
  "has_news":           false,
  "market_regime":      "strong",    // "strong" | "neutral" | "weak"
  "consecutive_losses": 0,
  "daily_pnl":          0.0,
  "market_cap_band":    "mega",      // equity-only: "small" | "mid" | "large" | "mega"
  "float_millions":     1000.0       // equity-only; for small-cap doctrine routing
}
```

| Field | Required? | If missing | Read by |
|---|---|---|---|
| `spread_bps` | **YES** | `roadguard_spread_floor` fail-closes (gate 7) | gate chain |
| `price` | recommended | observation_receipt has no anchor → unresolvable | resolver |
| `relative_volume` | recommended | sidecar quality drops to neutral; loses RVOL labels | doctrine labeler |
| `gap_pct` | recommended | loses GAPPER labels | doctrine labeler |
| `has_news` | optional | no catalyst bonus | doctrine labeler |
| `market_regime` | recommended | governor uses neutral risk_multiplier | doctrine governor |
| `consecutive_losses` | recommended | governor doesn't dampen on streak | doctrine governor |
| `volume` | optional | audit-only | audit |
| `market_cap_band` | equity only | mega-caps route to small-account doctrine (wrong) | lane router |
| `float_millions` | equity only | small-cap doctrine labels degrade | labeler |

### Forward-compat

`doctrine_snapshot` is an arbitrary dict. MC reads known keys and persists unknown keys
verbatim. **You may add new fields without coordinating with MC** — they round-trip into
the persisted intent doc for later analysis.

### The `evidence` field — brain honesty telemetry

This is where you tell MC about your INTERNAL state — separate from the outward
`action`/`confidence` you committed to.

```json
"evidence": {
  "raw_confidence":            0.1987,    // pre-personality/dampener confidence
  "size_multiplier":           0,         // your actual sizing decision [0.0, 1.0]
  "would_trade_without_gates": false,     // would you fire if MC didn't exist?
  "conviction_tier":           "MODERATE",
  "direction":                 "BUY"      // your direction label (may diverge from action)
}
```

This is the field that controls **observation_receipt creation**. If `action` is
directional and `evidence.size_multiplier == 0` (or `would_trade_without_gates == false`),
MC writes a graded learning sample. This is how the brain "learns without fills" — see
section 8.

### Honesty trailer (optional but recommended)

Any other audit / debugging fields you want preserved. MC will round-trip them on the
intent doc.

```json
"raw_action":           "BUY",       // before any post-processing
"execution_decision":   "self_zero", // what the brain actually decided
"narrative":            "tape weak, declining to size"
```

### Complete example

```json
{
  "stack":      "camaro",
  "action":     "BUY",
  "symbol":     "BNB/USD",
  "lane":       "crypto",
  "confidence": 0.677,
  "rationale":  "BNB breakout above 580 with elevated tape; mild mean-reversion bias",
  "doctrine_snapshot": {
    "spread_bps":      12,
    "price":           580.40,
    "volume":          12500000,
    "relative_volume": 2.3,
    "gap_pct":         1.5,
    "has_news":        false,
    "market_regime":   "strong",
    "consecutive_losses": 0,
    "daily_pnl":          0.0
  },
  "evidence": {
    "raw_confidence":            0.1987,
    "size_multiplier":           0,
    "would_trade_without_gates": false,
    "conviction_tier":           "MODERATE",
    "direction":                 "BUY"
  }
}
```

### Response shape

```json
{
  "intent_id":   "c90509c3-...",
  "stack":       "camaro",
  "symbol":      "BNB/USD",
  "action":      "BUY",
  "lane":        "crypto",
  "gate_state":  "pending",
  "snapshot":    { ... your doctrine_snapshot ... },
  "ingest_ts":   "2026-02-18T18:24:00Z"
}
```

`gate_state="pending"` is normal. The auto-router processes pending intents every 30s.

---

## 5. What MC does with your intent

```
                       ┌────────────────────┐
   POST /api/intents → │  shared_intents    │ ← gate_state=pending
                       └────────────────────┘
                                  ↓ (every 30s)
                       ┌────────────────────┐
                       │   auto_router      │
                       └────────────────────┘
                                  ↓
                       ┌────────────────────┐
                       │ classify_brain_    │
                       │    intent          │
                       └────────────────────┘
                                  ↓
              ┌───────────────────┼───────────────────┐
              ↓                   ↓                   ↓
        directional &       directional &        non-directional
        size > 0            size == 0            (HOLD / etc)
              ↓                   ↓                   ↓
        12-gate chain      observation_       silent
              ↓             receipt             advisory
        route_order
              ↓
        broker fill
              ↓
        execution_receipt
```

---

## 6. The 12-gate chain (what MC checks before routing)

For directional intents with size > 0:

1. `schema_invariants` — required fields present
2. `action_routable` — HOLD never becomes trade
3. `executor_seat_check` — your `stack` held the lane's executor seat at ingest time
4. `live_trading_disabled` — pass-through (defanged 2026-05-20)
5. `broker_connected` — Alpaca / Kraken adapter loaded
6. `lane_execution_enabled` — operator's per-lane kill switch ON
7. `roadguard_spread_floor` — `snapshot.spread_bps` ≤ lane cap (50 bps equity, 200 bps crypto)
8. `governor_authority` — governor seat held (Chevelle) AND not vetoing
9. `opponent_objection` — adversary seat advisory
10. `cap_per_order` / `cap_per_order_crypto` — notional ≤ per-order cap
11. `cap_per_day` — daily notional sum ≤ cap
12. `cap_open_notional` — total open positions ≤ cap

If any gate fails, MC writes a `gate_blocked` row with the typed reason. Auto-router
retries on next tick (currently re-evaluates — see backlog for "flip to blocked terminally"
fix).

---

## 7. Identity check-in (sidecar survival layer)

Every 30-60s, POST your `RuntimeStamp` to:

```
POST /api/admin/runtime/sidecar-checkin/<your_brain>
```

### Required fields

```json
{
  "stamp": {
    "app_name":               "risedual",
    "env_name":                "prod",                         // EXACT string. NOT "production".
    "git_sha":                 "abc123",
    "platform":                "emergent",
    "mc_url":                  "https://mission.risedual.ai",
    "db_name":                 "risedual_db",
    "broker_mode":             "paper",                        // "paper" | "live" | "simulated"
    "sidecar_room":            "alpha",
    "sidecar_version":         "1.0.0",
    "policy_hash":             "<sha256 of your decision policy>",
    "local_execution_authority": false,                        // MUST be false (doctrine)
    "timestamp_ms":            1700000000000

    // Forward-compat extras (tolerated, persisted, never break validation):
    // "pip_fingerprint": { ... },
    // "container_image":  "...",
    // any new field you want to surface
  }
}
```

### Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `verdict=preview` in MC | `env_name != "prod"` (case-sensitive, no whitespace) | Set `RISEDUAL_ENV=prod` exactly |
| `verdict=preview` | `mc_url` doesn't start with `https://mission.risedual.ai` | Check your `RISEDUAL_MC_URL` env var |
| `verdict=invalid` | `STAMP_SHAPE_INVALID` — unknown keyword that's actually a typo of a required field | Spell required field names exactly |
| `verdict=never` | Pod has never POSTed | Wire your `mc_checkin` task into startup |

`local_execution_authority: false` is a doctrine pin. A brain claiming `true` would be
rejected as `INVALID` by MC. Brains never have execution authority; MC does.

---

## 8. Observation receipts — how you learn without fills

If your brain emits a directional `action` (BUY/SELL/SHORT/COVER) with
`confidence ≥ 0.30` but you self-zero the size (`evidence.size_multiplier == 0` OR
`evidence.would_trade_without_gates == false`), MC writes an **observation receipt**.

This is graded learning **without capital risk**.

```
Your intent ─────────────────────────────────────────────────────────────────────────────
{ action: BUY, confidence: 0.677, evidence: { size_multiplier: 0 } }
        ↓
        ↓ (MC creates this; brain never POSTs receipts directly)
        ↓
observation_receipt {
  receipt_type: "observation_fill",
  synthetic: true,
  eligible_for_learning: true,
  eligible_for_live_unlock: false,
  anchor_price: <from snapshot>,
  resolved: false
}
        ↓
        ↓ (resolver, every 5min, fetches market prices at +1h / +4h / +1d / +5d)
        ↓
resolved: true, outcome: "win" | "loss" | "neutral", pnl_pct, mae_pct, mfe_pct
        ↓
        ↓ (counted toward your ladder progress)
        ↓
GET /api/admin/learning-ladder shows your (brain, lane) progress toward unlock
```

### Reading your progress

```
GET /api/admin/observation-receipts?brain=camaro&lane=crypto
GET /api/admin/observation-receipts/counts
GET /api/admin/learning-ladder
```

Operator manually promotes you up the ladder when criteria are met. You never push for
your own promotion.

---

## 9. The ladder

```
observation_only  →  micro_paper  →  micro_live  →  normal_live
```

| Stage | Unlocked by | Trade size |
|---|---|---|
| observation_only | (default) | 0 (synthetic receipts only) |
| micro_paper | 100 resolved obs + win_rate > 0.55 | Alpaca paper, capped notional |
| micro_live | 50 micro_paper fills + expectancy_R > 0.30 | Kraken USDC, ≤ $5 |
| normal_live | Operator decision | Full brain sizing within caps |

**You never POST to ladder endpoints. The ladder is operator-owned.** Your only job is to
emit honest opinions. The samples accumulate, operator reviews, operator promotes.

---

## 10. Things you must NOT do

* ❌ POST observation_receipts, execution_receipts, fills, or any graded data to MC.
* ❌ POST ladder transitions, promotion requests, or unlock claims.
* ❌ Submit orders directly to brokers. MC's auto_router does that.
* ❌ Set `local_execution_authority: true` in your stamp.
* ❌ Override MC's gates or claim to bypass them.
* ❌ Send `action: "HOLD"` and expect a trade. HOLDs are doctrinally non-trades.
* ❌ Use synthetic / fabricated `doctrine_snapshot.spread_bps` to spoof past gate 7.
  Send `9999.0` honestly if you don't have a real spread.

---

## 11. Things you SHOULD do

* ✅ Emit directional opinions with honest size_multiplier = 0 when conviction is real but weak.
  These ARE graded learning samples and they're how you climb the ladder.
* ✅ POST HOLDs when you actually mean HOLD. They get audited for completeness.
* ✅ Populate `doctrine_snapshot.spread_bps` with real market data (use a sentinel like
  `9999.0` if you genuinely can't get a spread — that fails gate 7 with `SPREAD_CAP`,
  which is honest, instead of `MISSING_SPREAD_BPS`, which looks broken).
* ✅ Stamp `env_name: "prod"` exactly. Whitespace and case matter.
* ✅ Tolerate MC restarts gracefully. Heartbeat ≥ every 60s.
* ✅ Add new fields to your stamp / snapshot without asking. MC tolerates unknowns and
  persists them.

---

## 12. Self-verification

After deploying any change to your brain, run these four checks. They take 30 seconds.

```bash
# 1. Your sidecar identity is healthy
curl https://mission.risedual.ai/api/admin/runtime/sidecar-checkin/<your_brain> \
  -H "Authorization: Bearer <operator_jwt>"
# Expect: verdict=prod, policy_hash_match=true

# 2. Your latest intent persisted snapshot correctly
curl "https://mission.risedual.ai/api/intents?stack=<your_brain>&limit=1" \
  -H "X-Runtime-Token: <YOUR_TOKEN>"
# Expect: items[0].snapshot.spread_bps populated, not null

# 3. Your ladder row shows real progress
curl https://mission.risedual.ai/api/admin/learning-ladder \
  -H "Authorization: Bearer <operator_jwt>"
# Expect: your (brain, lane) row carries progress numbers, not all zeros

# 4. Diagnostics liveness
curl https://mission.risedual.ai/api/admin/diagnostics \
  -H "Authorization: Bearer <operator_jwt>"
# Expect: runtimes[<your_brain>].heartbeat_tier == "ok"
```

If any of the four reads "wrong", the brain side is wrong. Don't ask MC to loosen
validation. The contract is the contract.

---

## 13. Versioning policy

This file is versioned in the MC repo's history. Material doctrine changes (new gates,
new required fields, new prohibitions) get a new dated section at the top instead of
in-place edits.

Brain teams should `git pull` this file weekly during the early ladder phases.

---

## 14. Where to file issues

If MC is doing something that contradicts this spec, open an issue with:

* Brain name + version
* Exact intent JSON that produced unexpected behavior
* The intent_id returned by `/api/intents`
* What you expected vs. what MC did

Don't open issues asking MC to loosen contracts. Doctrine is intentional.

---

## Appendix: Doctrine pins quoted from MC source

These pins live in `/app/backend/shared/` and are the authoritative answer to "why does MC
behave this way?":

```
shared/execution.py:
    Doctrine (c, 2026-05-20): governor never hard-blocks; modulates only.
    `risk_multiplier` floors at 0.10.

shared/intents.py:
    READ-ONLY ATTACHMENT — `doctrine_snapshot` never modifies direction,
    confidence, or any gate state.

shared/lane_execution.py:
    Operator-owned kill switch per lane. Defaults OFF. Decoupled from
    broker credential state.

shared/observation_receipts.py:
    Observation receipts are SYNTHETIC. No broker, no money. Graded
    by MC against future market price. Brain never POSTs receipts.

shared/learning_ladder.py:
    Stage transitions are operator-gated. Auto-promotion eligibility
    is COMPUTED but never auto-triggered. Capital risk must be
    deliberate.
```
