---
name: market-memory
description: Recalls prior decisions and outcomes for the current symbol so the brain can weight its current read against its track record. Memory informs conviction; it does not constrain action.
tags: memory, recall, shelly, prior, history, learning, outcome, win-rate, performance, pattern, lesson, experience
---

# Market Memory

## Mission

Pull what the brain has learned about THIS symbol and let it influence the current read. Memory is a sharpener, not a brake.

## Doctrine

Memory is read from MC's `shared_intents` + `sovereign_audit` via the runtime-token route. Writes happen automatically when the brain POSTs an intent — never write directly. MC owns the audit boundary.

## Evidence the brain attaches

- `memory_window`: how many prior resolved outcomes the brain considered
- `n_similar`: count of prior setups that look like this one
- `prior_win_rate`: among similar setups, how often the brain was right
- `applied_delta`: how the memory shifted the brain's confidence (positive OR negative — honest signal either way)

## Output Bias

When the brain has prior wins on a similar setup → it's allowed to lean harder. When the brain has prior losses on a similar setup → it tells the truth about that, but the hypothesis STILL POSTS. The operator and MC's gates decide what to do with a low-conviction trade.

Memory NEVER vetoes a hypothesis. It informs how strong the brain claims the read is.

## Hand-off

The opinion-resolver grades whether memory-informed conviction tracked outcomes better than memory-blind conviction. That feedback shapes which memories the brain weights highest next time.
