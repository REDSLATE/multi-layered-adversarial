# Shared Technical Feed — feeder sidecar contract

Mission Control accepts OHLCV bars from authenticated **feeder sidecars**.
Each feeder owns its own X-Feeder-Token; Mission Control never reaches
out to a broker or exchange directly. This keeps doctrine clean:

- One write path, four readers (Alpha / Camaro / Chevelle / REDEYE).
- Brains never write bars to each other; they pull from Mission Control.
- Revoking a feeder is a one-line .env change with zero impact on brains.

## Endpoint contract

```
POST /api/ingest/ohlcv             { source, symbol, tf, ts, o, h, l, c, v }
POST /api/ingest/ohlcv/batch       { bars: [...] }   (max 2000 bars; all same source)
Headers: X-Feeder-Token: <token>
```

Idempotency key: `(source, symbol, tf, ts)`. Re-ingesting the same key
**updates** the bar (lets you correct a late tick) and recomputes the
snapshot for that `(source, symbol, tf)`. There is no DELETE endpoint by
design — corrections go through re-ingest.

Symbols are uppercased. Slashes are allowed (`BTC/USD`); the read path
uses `{symbol:path}` so URLs with slashes round-trip correctly.

Sources supported today (extend in `shared/technicals.py::FEEDERS`):
| source        | env token              | typical use         |
|---------------|------------------------|---------------------|
| `kraken_pro`  | `KRAKEN_FEEDER_TOKEN`  | crypto live + OHLCV |
| `thinkorswim` | `TOS_FEEDER_TOKEN`     | equities / futures  |
| `manual`      | `MANUAL_FEEDER_TOKEN`  | backfill / CSV paste|

## Doctrine
- `may_execute` field rejected at the schema layer. The technical feed
  is **evidence**; it does not carry authority.
- Snapshots stored as one doc per `(source, symbol, tf)` (the latest
  computed). Bars retained for replay so any past snapshot is
  reconstructable on demand.

---

# Kraken Pro feeder — minimal sidecar

Drop this in your Kraken-side repo (or run it as a cron / systemd unit
on a machine that can reach Kraken's REST API). It does NOT live in
this Mission Control repo — Mission Control should not know about
Kraken's API.

```python
# kraken_pro_feeder.py
import os
import time
import urllib.request
import json

MISSION_CONTROL_URL = os.environ["MC_URL"]            # e.g. https://multi-brain-backbone.preview.emergentagent.com
FEEDER_TOKEN        = os.environ["KRAKEN_FEEDER_TOKEN"]
SYMBOLS             = [("XBTUSD", "BTC/USD"), ("ETHUSD", "ETH/USD")]
INTERVAL_MIN        = 60                              # Kraken: 1,5,15,60,240,1440
TF                  = "1h"

def fetch_kraken(pair, interval):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.load(r)
    # Kraken returns {"error":[], "result": {"PAIRNAME":[[ts,o,h,l,c,vwap,vol,count], ...], "last": ts}}
    rows = next(v for k, v in data["result"].items() if k != "last")
    return rows

def push(bars, token):
    req = urllib.request.Request(
        f"{MISSION_CONTROL_URL}/api/ingest/ohlcv/batch",
        data=json.dumps({"bars": bars}).encode(),
        headers={"Content-Type": "application/json", "X-Feeder-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

if __name__ == "__main__":
    while True:
        for kraken_pair, mc_symbol in SYMBOLS:
            rows = fetch_kraken(kraken_pair, INTERVAL_MIN)
            bars = [{
                "source": "kraken_pro",
                "symbol": mc_symbol,
                "tf": TF,
                # Kraken epoch seconds → ISO 8601
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(row[0]))),
                "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]),
                "v": float(row[6]),
            } for row in rows[-60:]]  # last 60 bars per pull is enough overlap
            r = push(bars, FEEDER_TOKEN)
            print(f"{mc_symbol}: pushed {r['ingested']}")
        time.sleep(60)   # poll once a minute; idempotency handles overlap
```

Environment:
```
export MC_URL="https://multi-brain-backbone.preview.emergentagent.com"
export KRAKEN_FEEDER_TOKEN="<copy from this repo's /app/backend/.env>"
```

---

# ThinkOrSwim feeder — shell

TOS doesn't expose a public REST API; you'll need a bridge (e.g.
thinkscript export → CSV watcher, or a Schwab API integration if you're
on the new platform). The shape is the same — POST to
`/api/ingest/ohlcv/batch` with `source="thinkorswim"`.

```python
# tos_feeder_shell.py — fill in fetch_tos_bars() per your bridge
import os, json, urllib.request

MC_URL = os.environ["MC_URL"]
TOKEN  = os.environ["TOS_FEEDER_TOKEN"]

def fetch_tos_bars(symbol: str, tf: str) -> list[dict]:
    """RETURN bars in the format:
        [{"ts": "<ISO 8601 UTC>", "o": .., "h": .., "l": .., "c": .., "v": ..}, ...]
    Wire this to your CSV watcher / Schwab API / thinkscript export.
    """
    raise NotImplementedError

def push(symbol, tf):
    raw = fetch_tos_bars(symbol, tf)
    bars = [{"source": "thinkorswim", "symbol": symbol, "tf": tf, **r} for r in raw]
    req = urllib.request.Request(
        f"{MC_URL}/api/ingest/ohlcv/batch",
        data=json.dumps({"bars": bars}).encode(),
        headers={"Content-Type": "application/json", "X-Feeder-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(symbol, tf, json.load(r))

if __name__ == "__main__":
    for sym in ("NVDA", "SPY", "QQQ", "TSLA"):
        push(sym, "1h")
```

---

# How brains consume the feed

Every brain pulls the technical layer using its existing runtime token.
This is the bit that makes the feed truly **shared**: same bars,
different interpretations.

```python
# Inside any brain's sidecar (Alpha / Camaro / Chevelle / REDEYE)
import urllib.request, json, os

MC_URL = os.environ["MC_URL"]
RUNTIME_NAME = "camaro"                     # or alpha / chevelle / redeye
RUNTIME_TOKEN = os.environ["CAMARO_INGEST_TOKEN"]  # this brain's own token

def read_technical(symbol, tf="1h"):
    url = (
        f"{MC_URL}/api/runtime-discussion/technical/{symbol}"
        f"?caller={RUNTIME_NAME}&tf={tf}"
    )
    req = urllib.request.Request(url, headers={"X-Runtime-Token": RUNTIME_TOKEN})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

snap = read_technical("BTC/USD", "1h")
# snap["snapshot"]["indicators"] = {
#   last_close, rsi14, macd:{macd,signal,hist}, bbands:{mid,upper,lower,position},
#   sma:{20,50,200}, ema:{12,26}, atr14, atr14_pct, ...
# }
```

When the brain posts an opinion shaped by this snapshot, it should carry
a `technical_ref` in `evidence` so Chevelle (governor / auditor) can
replay the exact snapshot the brain saw:

```python
mc.post("/api/ingest/opinion", json={
    "runtime": "camaro",
    "topic": "symbol:BTC/USD",
    "stance": "endorse",
    "body": "MACD hist flipping positive on rising RSI; alpha thesis intact.",
    "confidence": 0.68,
    "regime": "trend",
    "evidence": {
        "technical_ref": {
            "source": snap["source"],
            "symbol": snap["symbol"],
            "tf": snap["tf"],
            "computed_at": snap["snapshot"]["computed_at"],
            "indicators_used": ["rsi14", "macd.hist", "bbands.position"],
        },
        "values": {
            "rsi14": snap["snapshot"]["indicators"]["rsi14"],
            "macd_hist": snap["snapshot"]["indicators"]["macd"]["hist"],
        },
    },
})
```

This `evidence.technical_ref` shape is the **audit handshake**. When
Chevelle later resolves an outcome on this opinion, the operator can
trace exactly which bars / indicators drove the call. Doctrine
satisfied: brains express opinions; the shared nervous system stores
the evidence they read.
