---
name: adversarial-risk
description: Surfaces the strongest opposing view of the current setup as evidence on the hypothesis. Sharpens conviction by stress-testing it, not by softening it.
tags: opponent, downside, bear, bear-case, disagreement, reversal, trap, squeeze, contrarian, counter, alternative
---

# Adversarial Risk

## Mission

Generate the strongest counter-read of the current setup. Attach it as evidence. The brain decides what to do with it — defending its read sharpens conviction; failing to defend it lowers conviction honestly.

## Doctrine

The counter-read is information, not a veto. The brain still emits a hypothesis; MC decides routing.

## Evidence the brain attaches

- `counter_thesis`: a one-line alternative read (e.g., "wedge breakdown setup — supply at +1.2% above")
- `counter_evidence`: 1–3 signals supporting the counter-thesis (divergence, fading volume, level confluence, etc.)
- `counter_strength`: brain's own scoring of how persuasive the counter is (0.0–1.0)

## Output Bias

- Strong counter that the brain CAN'T dismiss → confidence reflects that honesty (lower, but still post the hypothesis).
- Strong counter that the brain CAN dismiss with specific evidence → confidence stays high; the dismissal is attached as `counter_dismissed_because: ...`.
- Weak counter → no adjustment.

Conviction modulation here is the brain being honest with itself, not a guard.

## Hand-off

MC logs counter-thesis evidence on the intent. The opinion-resolver eventually grades whether the counter was the right call. Brains learn over time which counter-pattern matters.
