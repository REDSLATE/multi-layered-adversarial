---
name: risk-perception
description: Identifies risk vectors as evidence on the hypothesis — drawdown context, exposure context, spread context — so the operator can see them in the audit, not so the brain throttles itself.
tags: risk, exposure, drawdown, spread, liquidity, context, environment, perception, awareness
---

# Risk Perception

## Mission

See the risk landscape clearly and attach it as evidence. The brain's job is to NOTICE and REPORT. The operator runs the gates.

## Doctrine

This skill produces evidence, not enforcement. It NEVER halts, dampens, or holds. Every restriction lives in MC (lane toggles, exposure caps, sizing_gate, ladder) where the operator can flip it at runtime.

## Evidence the brain attaches

- `spread_bps_observed`: the spread the brain actually saw
- `lane_drawdown_window_pct`: any recent drawdown in the brain's lane
- `recent_resolved_outcomes`: last N resolved outcomes for context (count + direction, no enforcement)
- `liquidity_signal`: brain's read on orderbook depth / volume freshness

## Output

The skill enriches the hypothesis's `evidence` block. The hypothesis's action and confidence are owned by the trading skill (crypto-execution, etc.), not by this one.

## Hand-off

MC's audit log captures every risk-perception evidence row. The opinion-resolver eventually correlates which evidence patterns predicted bad outcomes — that feeds back into reputation, not into a code-side guard.
