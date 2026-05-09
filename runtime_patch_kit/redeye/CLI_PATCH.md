# REDEYE CLI Patch — `shorts` command

Three small additions to REDEYE's `risedual` CLI module. Paste in order.

---

## 1) Imports — near the other imports at the top of the CLI file

If REDEYE's CLI lives inside the `risedual` package (most likely):

```python
from .services.redeye_short_bridge import build_redeye_short_signal, export_for_camaro
```

If your CLI uses absolute imports:

```python
from services.redeye_short_bridge import build_redeye_short_signal, export_for_camaro
```

You also need `json` already imported (`import json`). Most CLIs already have it.

---

## 2) Command function — alongside the other `cmd_*` handlers

```python
def cmd_shorts(args) -> int:
    """
    REDEYE short-side scout.

    Reports to Camaro.
    Does not execute.
    """

    features = {
        "price_change_pct": args.price_change_pct,
        "rsi_14": args.rsi,
        "macd_hist": args.macd_hist,
        "volume_ratio": args.volume_ratio,
        "below_sma_20": args.below_sma_20,
        "below_sma_50": args.below_sma_50,
        "failed_bounce": args.failed_bounce,
        "liquidity_ok": not args.liquidity_block,
        "borrow_ok": not args.borrow_block,
    }

    signal = build_redeye_short_signal(
        args.symbol,
        features,
        model_score=args.model_score,
    )

    payload = export_for_camaro(signal)

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
```

---

## 3) Subparser — inside `build_parser()`, before `return p`

```python
    # shorts
    sp = sub.add_parser("shorts", help="REDEYE short-side scout; reports to Camaro")
    sp.add_argument("--symbol", required=True)
    sp.add_argument("--price-change-pct", type=float, default=0.0)
    sp.add_argument("--rsi", type=float, default=50.0)
    sp.add_argument("--macd-hist", type=float, default=0.0)
    sp.add_argument("--volume-ratio", type=float, default=1.0)
    sp.add_argument("--below-sma-20", action="store_true")
    sp.add_argument("--below-sma-50", action="store_true")
    sp.add_argument("--failed-bounce", action="store_true")
    sp.add_argument("--liquidity-block", action="store_true")
    sp.add_argument("--borrow-block", action="store_true")
    sp.add_argument("--model-score", type=float, default=None)
    sp.set_defaults(func=cmd_shorts)
```

---

## Smoke test (run inside REDEYE after patching)

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

Expected:

```json
{
  "action": "SHORT",
  "engine": "REDEYE",
  "reports_to": "CAMARO",
  "camaro_contract": {
    "final_authority": "CAMARO",
    "may_execute": false,
    "may_override_alpha": false,
    "role": "short_side_advisor",
    "source": "REDEYE"
  }
}
```

(Other fields — `bear_score`, `confidence`, `risk_multiplier`, `reason`, `created_at`, `raw` — are also emitted.)
