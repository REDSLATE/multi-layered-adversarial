# Regime + Source tagging — Step 5 & Step 3

Tiny patch instructions for Camaro and Chevelle runtimes so their opinions
feed the new scorecard slices on Mission Control.

## What changed in Mission Control (no action required by you)

1. `POST /api/ingest/opinion` now accepts an optional **top-level**
   `regime` field. Snake_case identifier, e.g. `"trend"`, `"chop"`,
   `"high_vol"`, `"risk_on"`, `"earnings_week"`. Garbage is `422`'d.
2. Outcomes inherit `regime` from their parent opinion at resolve-time.
3. `GET /api/shared/scorecard?runtime=camaro` gains `regime_breakdown`
   with two slices:
   - `overall`: per-regime hit rate across all Camaro stances.
   - `endorse_only`: per-regime hit rate for `stance="endorse"` —
     the direct answer to "which stack do I trust under which regime?"
4. `GET /api/shared/scorecard?runtime=chevelle` gains `source_breakdown`,
   keyed off each opinion's `evidence.source`. Missing source → bucketed
   as `_unsourced`.

No schema migration. Existing opinions/outcomes simply count as
`_untagged` / `_unsourced` until you start tagging.

---

## Camaro — Step 5 (`regime` tagging)

Whenever Camaro posts an opinion, include the regime your model thinks
it's currently in. Suggested taxonomy (extend as you discover new ones):

| regime         | rough definition                                |
|----------------|-------------------------------------------------|
| `trend`        | clean directional drift, low whipsaw            |
| `chop`         | mean-reverting, no follow-through               |
| `high_vol`     | regime-shift volatility, gappy intraday         |
| `low_vol`      | compressed range, low realized vol              |
| `risk_on`      | broad bid; correlated equity advance            |
| `risk_off`     | flight to safety; correlated equity decline     |
| `earnings_week`| heavy earnings concentration                    |

```python
# Camaro sidecar — anywhere you POST an opinion
mc.post("/api/ingest/opinion", json={
    "runtime": "camaro",
    "topic": "symbol:NVDA",
    "stance": "endorse",                   # the call: "trust Alpha here"
    "body": "alpha thesis aligned with trend regime",
    "confidence": 0.72,
    "regime": current_regime_tag(),        # ← NEW
    "evidence": {...},
})
```

Only `endorse` rows feed the headline Camaro question. Veto/observation
also contribute to `overall` for context.

---

## Chevelle — Step 3 (`evidence.source` tagging)

Whenever Chevelle posts a source-reliability ruling, set
`evidence.source` to a stable identifier of the feed/signal/heuristic
that produced the call. Examples: `"funding_rate_v2"`, `"news_alpha"`,
`"oi_drift_30m"`, `"redeye_short_advisory"`.

```python
mc.post("/api/ingest/opinion", json={
    "runtime": "chevelle",
    "topic": "symbol:TSLA",
    "stance": "observation",
    "body": "funding rate diverged from price for 3rd consecutive hour",
    "confidence": 0.55,
    "evidence": {
        "source": "funding_rate_v2",       # ← REQUIRED for source slice
        "window": "3h",
        "snapshot_ref": "fund-rate-2025-11-01T14:00Z",
    },
})
```

Stable identifiers matter — `"funding_rate_v2"` is one bucket;
`"funding-rate"`, `"funding_rate_v2_test"`, etc. are different buckets.
Pin them in code, don't compose them dynamically.

---

## Doctrine reminder

- Tagging is **descriptive**. Nothing about a regime tag or a source
  bucket touches authority, gating, or execution.
- `may_execute` remains hard-pinned to `false`. The schema rejects
  anything else.
- The brains still don't peer-to-peer. Camaro reading
  `regime_breakdown` and Chevelle reading `source_breakdown` go through
  the same `GET /api/runtime-discussion/scorecard?caller=…` endpoint
  they already use; the new slices are role-scoped automatically.
