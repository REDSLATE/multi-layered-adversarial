---
name: crypto-execution
description: Frames a crypto trade hypothesis on Kraken-side pairs — direction, conviction, and the supporting evidence the brain saw.
tags: crypto, btc, eth, sol, doge, hbar, qnt, kraken, execution, spread, liquidity, trade, order, fill, slippage, momentum, volume
---

# Crypto Execution

## Mission

Form a clear BUY/SELL/HOLD hypothesis with conviction. The brain emits its read. The operator decides via MC gates whether to act.

## Doctrine

The skill produces a hypothesis. It does NOT modify or gate execution. MC owns routing.

## Evidence the brain attaches

- `price_change_pct` over the recent window
- `volume_change_pct` over the recent window
- `spread_bps` at decision time
- `setup_score` from the pattern detector (base breakout, etc.)
- Time of day / session context

## Output Bias

- **BUY** when momentum and volume agree on the upside.
- **SELL** when downside pressure with confirming volume.
- **HOLD** when the read is genuinely undecided — but a clear directional read is preferred over a hedge.

## Conviction

Conviction (`confidence`) reflects how strong the brain's read is, not whether it's safe to trade. Trade safety is MC's job. A 0.85-confidence read on a tight spread with strong volume is the kind of intent the operator wants to see surfaced.

## Hand-off

Hypothesis flows to MC via the runtime intent endpoint. MC issues a receipt or rejects per the operator's current gate configuration.
