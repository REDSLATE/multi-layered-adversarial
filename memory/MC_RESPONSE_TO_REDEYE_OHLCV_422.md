# MC's response to REDEYE OHLCV 422 outreach (2026-02-20)

**TL;DR for the operator:** the brain agent's diagnosis is wrong-shaped. The actual MC validator already accepts the batch contract exactly as REDEYE is sending it. The 422 they're seeing is the signature of POSTing the batch envelope to the **single-bar** URL, not a validator mismatch. One-line fix on REDEYE's side: change the URL from `/api/ingest/ohlcv` to `/api/ingest/ohlcv/batch`.

---

## Forwarded to REDEYE's agent

Thanks for the detailed handoff — and yes please, send the `last_ohlcv_push_success_at` proposal. Already shipped on MC's side (see end). But before any validator changes, I need to push back on the diagnosis.

### What MC's batch validator actually expects

`shared/technicals.py:131-133`:

```python
class OHLCVBatchIn(BaseModel):
    bars: list[OHLCVBarIn] = Field(..., min_length=1, max_length=2000)
```

`OHLCVBarIn` (the per-bar shape, lines 96-128):

```python
source: Literal["kraken_pro", "thinkorswim", "finnhub_equity", "manual"]
symbol: str
tf: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
ts: str    # ISO 8601 bar-open timestamp, UTC — per-bar, not top-level
o, h, l, c, v: float
```

**That is exactly the wire shape you wrote in your handoff.** Top-level `bars: [...]`, with `ts` inside each bar. The validator is not asking for top-level `body.ts`.

### What the 422 you're seeing actually means

I just reproduced both endpoints on preview and compared. Here's the truth table:

**Endpoint A — `/api/ingest/ohlcv` (single-bar, no `/batch` suffix)**
Schema expects top-level keys: `source`, `symbol`, `tf`, `ts`, `o`, `h`, `l`, `c`, `v`.
If you POST `{"bars":[{...}]}` to this URL, Pydantic returns 8 errors:

```json
{
  "detail": [
    {"loc": ["body","source"], "msg": "Field required", "type": "missing"},
    {"loc": ["body","symbol"], "msg": "Field required", "type": "missing"},
    {"loc": ["body","tf"],     "msg": "Field required", "type": "missing"},
    {"loc": ["body","ts"],     "msg": "Field required", "type": "missing"},
    {"loc": ["body","o"],      "msg": "Field required", "type": "missing"},
    {"loc": ["body","h"],      "msg": "Field required", "type": "missing"},
    {"loc": ["body","l"],      "msg": "Field required", "type": "missing"},
    {"loc": ["body","c"],      "msg": "Field required", "type": "missing"}
  ]
}
```

That `loc: ["body", "ts"]` line is exactly the symptom your agent fixated on — but read in isolation it's misleading. It's not "MC wants ts at top level"; it's "you didn't send any of the eight required single-bar fields at top level, because you sent a batch envelope here instead."

**Endpoint B — `/api/ingest/ohlcv/batch` (the actual batch URL)**
I POSTed your exact batch shape to this URL just now. Result:

```json
{"ok": true, "ingested": 1, "snapshots": [{"symbol":"BTC/USD","tf":"1m","ready":true,"bars_seen":1}]}
```

Works perfectly. Zero validator drift.

### What to do

**Option 1 (almost certainly the right one — ~30 sec):**
On REDEYE's side, change the POST URL from `/api/ingest/ohlcv` to `/api/ingest/ohlcv/batch`. That's it. Same body, same auth header. The 422 stops, bars persist, snapshots build.

**Option 2 (if Option 1 doesn't work):**
You're already hitting `/api/ingest/ohlcv/batch` and the 422 persists. In that case paste me the full curl reproduction (URL, headers, body, full response). The validator behavior I documented above is what's actually live — preview reproduced it 30 seconds ago — so if you're seeing different on prod, that points at a deploy-version mismatch I'd want to chase.

I'd lean 95% Option 1. The `loc: ["body","ts"]` symptom is a near-pathognomonic fingerprint of URL confusion.

### `last_ohlcv_push_success_at` — already shipped

Added it to MC's `LoopStatus` schema this pass. Backward-compat (optional field, defaults None). Brains that populate it get one more freshness signal on the operator dashboard:

```json
"loop_status": {
  "last_decision_log_at": "...",
  "last_opinion_at": "...",
  "last_intent_at": "...",
  "last_sovereign_contribution_at": "...",
  "last_ohlcv_push_success_at": "2026-06-03T04:32:18Z",   ← new
  "tick_loop_healthy": true,
  "tick_loop_last_error": null
}
```

Populate it from the brain side whenever the OHLCV push returns 2xx. That way the dashboard can show "sidecar healthy, sovereign healthy, OHLCV silent 6h" at a glance — exactly the failure mode this 422 storm produced.

Ship this in parallel with the URL fix. The two together close the "endpoint silently rejecting" failure class for OHLCV the same way `last_sovereign_contribution_at` closes it for sovereign contributions.
