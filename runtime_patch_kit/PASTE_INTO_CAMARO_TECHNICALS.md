# Camaro · Technical-evidence pull pattern

> **Audience**: the Camaro sidecar (your one runtime that actually has a sidecar today).
> The other three brains are unwired for now — when they get sidecars later, they
> can paste the same pattern.

This is the contract that turns the shared technical feed from "data in
the system" into "evidence that drove a decision." Camaro pulls a
snapshot **before** posting an opinion, attaches a `technical_ref` to
the opinion, and Mission Control can later replay the exact indicator
values Camaro saw — even minutes, hours, or days later.

## Doctrine reminder

- Pulling technical evidence does NOT grant execution authority.
  `may_execute` stays schema-pinned `false` on every opinion.
- Camaro's job remains *judgement*: trust / reduce / veto / observation.
  The technicals are inputs, not orders.
- Every opinion that quotes technical values **must** carry
  `evidence.technical_ref` so Chevelle (governor) can audit-replay.

---

## Step 1 — Read the snapshot

```python
# camaro/mc_client.py — drop-in helper
import os, json, urllib.request, urllib.parse

MC_URL        = os.environ["MC_URL"]                      # https://mission.risedual.ai
RUNTIME_NAME  = "camaro"
RUNTIME_TOKEN = os.environ["CAMARO_INGEST_TOKEN"]         # from Mission Control's .env


def read_technical(symbol: str, tf: str = "1h", source: str | None = None) -> dict:
    """Pull the latest indicator snapshot for `symbol` / `tf`.

    Returns the full payload — caller passes `snapshot["computed_at"]`
    forward as `technical_ref.computed_at` when posting an opinion.
    """
    qs = {"caller": RUNTIME_NAME, "tf": tf}
    if source:
        qs["source"] = source
    url = (
        f"{MC_URL}/api/runtime-discussion/technical/{urllib.parse.quote(symbol, safe='/')}"
        f"?{urllib.parse.urlencode(qs)}"
    )
    req = urllib.request.Request(url, headers={"X-Runtime-Token": RUNTIME_TOKEN})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)
```

The returned `snapshot["indicators"]` looks like:

```jsonc
{
  "ready": true,
  "bars_seen": 300,
  "last_close": 80876.9,
  "sma":  {"20": ..., "50": ..., "200": ...},
  "ema":  {"12": ..., "26": ...},
  "rsi14": 47.1,
  "macd": {"macd": ..., "signal": ..., "hist": -91.4, "hist_tail": [...]},
  "bbands": {"mid": ..., "upper": ..., "lower": ..., "width_pct": ..., "position": 0.32},
  "atr14": ..., "atr14_pct": 0.49
}
```

## Step 2 — Form an opinion. Quote only what you used.

Camaro should keep its **evidence.values** narrow — only the indicators
that actually shaped the call. The richer `technical_ref` lets Chevelle
recompute everything else from the bars if it ever needs to.

```python
def post_opinion(topic: str, stance: str, body: str, confidence: float,
                 *, regime: str | None = None,
                 technical_ref: dict | None = None,
                 quoted_values: dict | None = None) -> dict:
    payload = {
        "runtime": RUNTIME_NAME,
        "topic": topic,                       # e.g. "symbol:BTC/USD"
        "stance": stance,                     # endorse | veto | observation
        "body": body,
        "confidence": confidence,
    }
    if regime is not None:
        payload["regime"] = regime
    evidence: dict = {}
    if technical_ref is not None:
        evidence["technical_ref"] = technical_ref
    if quoted_values is not None:
        evidence["values"] = quoted_values
    if evidence:
        payload["evidence"] = evidence
    req = urllib.request.Request(
        f"{MC_URL}/api/ingest/opinion",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Runtime-Token": RUNTIME_TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)
```

## Step 3 — Stitch it together (full example)

```python
# Camaro forms a judgement on BTC/USD using the live snapshot:
snap = read_technical("BTC/USD", "1h")
ind = snap["snapshot"]["indicators"]

# Camaro's own logic — interpret the indicators (this is Camaro-specific
# code that lives in your sidecar; Mission Control doesn't impose a
# decision tree).
if ind["macd"]["hist"] > 0 and ind["rsi14"] < 70 and ind["bbands"]["position"] < 0.85:
    stance = "endorse"
    body   = "MACD hist positive, RSI not overbought, in upper-mid band — Alpha thesis intact."
    conf   = 0.68
elif ind["rsi14"] > 75 or ind["bbands"]["position"] > 0.95:
    stance = "veto"
    body   = "Stretched into upper band with high RSI — wait for a pullback."
    conf   = 0.72
else:
    stance = "observation"
    body   = "Mixed read — no strong call this bar."
    conf   = 0.40

post_opinion(
    topic="symbol:BTC/USD",
    stance=stance,
    body=body,
    confidence=conf,
    regime="trend",                            # tag if you know the regime
    technical_ref={
        "source":      snap["source"],         # kraken_pro
        "symbol":      snap["symbol"],         # BTC/USD
        "tf":          snap["tf"],             # 1h
        "computed_at": snap["snapshot"]["computed_at"],
        "last_bar_ts": snap["snapshot"]["last_bar_ts"],
        "indicators_used": ["rsi14", "macd.hist", "bbands.position"],
    },
    quoted_values={
        "rsi14":          ind["rsi14"],
        "macd_hist":      ind["macd"]["hist"],
        "bb_position":    ind["bbands"]["position"],
    },
)
```

That's it. Three function calls per opinion: `read_technical → decide →
post_opinion`. Latency budget on Mission Control side is sub-300ms;
Camaro can post freely on every new bar.

---

## Audit replay — what this enables on the operator side

When you scroll the Discussion / Conflicts / Receipts pages and click on a
Camaro opinion that carries `evidence.technical_ref`, Mission Control
fetches:

```
GET /api/shared/technical/{symbol}?tf=&source=&as_of={technical_ref.computed_at}
```

That endpoint **recomputes** the indicator snapshot from retained bars
≤ `as_of` using the identical pure-function pipeline. The operator sees
the exact RSI / MACD / BB values Camaro had in front of it. Reproducible,
auditable, doctrine-compatible.

---

## Pitfalls / what NOT to do

1. **Don't post values you didn't compute.** If Camaro logic didn't
   look at `atr14_pct`, don't include it in `quoted_values`. The point
   of the audit is to know *what drove the call*.
2. **Don't read a snapshot, then sleep an hour, then post the opinion
   with the old `technical_ref.computed_at`.** Re-read just before
   posting — Camaro's intuition might fire on a bar that's already
   moved. Re-pulling is cheap.
3. **Don't substitute a different `source`** between read and post. If
   you read from `kraken_pro` and then post a ref pointing at
   `thinkorswim`, the audit will look like a contradiction.
4. **Don't synthesise indicators on Camaro's side.** Mission Control
   computes the canonical set. Camaro consumes them. If you need a new
   indicator (Ichimoku, anchored VWAP, etc.) ask Mission Control to add
   it — that keeps the shared evidence shared.

---

## When the other brains get sidecars

Same paste-in. Alpha, Chevelle, REDEYE each substitute their own
`RUNTIME_NAME` + ingest token. The endpoint is identical because the
*evidence* is shared. The *interpretation* lives in each sidecar.
