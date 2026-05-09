# REDEYE Patch Kit — Short-Side Bridge to Camaro

> **Doctrine.** REDEYE is a bearish/short-side adversarial scout. It is **not**
> a peer of Alpha or Chevelle. It reports to **Camaro only**. Camaro is the
> final live execution authority. REDEYE never bypasses Camaro and never
> talks to Alpha.

```
Alpha   ─────┐
             ├──► Camaro Commander ──► final live decision
REDEYE  ─────┘
```

The `camaro_contract` block on every emitted payload carries this
contract verbatim:

```json
{
  "source": "REDEYE",
  "role": "short_side_advisor",
  "may_execute": false,
  "may_override_alpha": false,
  "final_authority": "CAMARO"
}
```

---

## Files in this kit

| File | Purpose |
|---|---|
| `services/redeye_short_bridge.py` | The bridge module. Drop into REDEYE's `services/` package. |
| `CLI_PATCH.md` | Copy-paste instructions for wiring the `shorts` command into REDEYE's `risedual` CLI. |
| `smoke_test.py` | Standalone runner that exercises the bridge with the documented example. |

---

## STEP 1 — Drop the bridge into REDEYE

```
cp services/redeye_short_bridge.py  <REDEYE_REPO>/services/redeye_short_bridge.py
```

No new dependencies. Pure stdlib.

---

## STEP 2 — Wire the CLI

Open `CLI_PATCH.md` in this folder. It contains the three small patches for
REDEYE's `risedual` CLI module: imports, `cmd_shorts` function, and the
`shorts` subparser. Paste them in order.

---

## STEP 3 — Verify locally before pasting

From this folder:

```bash
python3 smoke_test.py
```

Expected output is a JSON payload with:

```json
{
  "engine": "REDEYE",
  "action": "SHORT",
  "reports_to": "CAMARO",
  "camaro_contract": {
    "may_execute": false,
    "may_override_alpha": false,
    "final_authority": "CAMARO"
  }
}
```

If the smoke test passes here, the same logic will run identically inside
REDEYE.

---

## STEP 4 — Run it from REDEYE

After pasting the CLI patch into REDEYE:

```bash
python -m risedual shorts \
  --symbol TSLA \
  --price-change-pct -2.4 \
  --rsi 39 \
  --macd-hist -0.22 \
  --volume-ratio 1.8 \
  --below-sma-20 \
  --below-sma-50 \
  --failed-bounce \
  --model-score 0.82
```

---

## What this kit deliberately does NOT do

- Does **not** open an ingest channel from REDEYE → Mission Control yet.
  REDEYE remains a separate brain; if/when its signals need to land in the
  shared dashboard, route them via Camaro's existing
  `risedual_monorepo_client` so the chain of custody stays
  `REDEYE → Camaro → shared_adl_receipts`.
- Does **not** add REDEYE as a 4th runtime in `namespaces.py`. REDEYE has no
  authority on the trading ladder; granting it one would violate the
  doctrine that REDEYE only ever advises Camaro.
- Does **not** execute orders. The bridge is advisory by design — `allowed=True`
  means "Camaro is allowed to consider this short", not "go place the trade".
