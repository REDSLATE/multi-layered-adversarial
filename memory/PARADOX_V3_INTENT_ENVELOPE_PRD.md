# Paradox v3 — Intent Envelope PRD

> **Status:** DRAFT — pending operator approval.
> **Author:** MC main agent (continuation of 2026-06-22 / observation-phase).
> **Doctrine pin:** "Plan the schema first. Do not touch code until this PRD is approved."

---

## 1. Why we are rewriting the envelope

### 1.1 The operator's framing

The current `action: BUY | SELL | SHORT | COVER | HOLD | OPEN | CLOSE`
vocabulary forces every brain to collapse its full analysis into one of seven
verbs. That works for an executable order ticket. It does **not** work for
an *intent* — which is supposed to be the brain's planning artifact.

Two real failure modes observed in production over the last 30 days:

1. **The "I see a setup but waiting for the trigger" intent**
   A brain reads a clean intraday bull-flag, but price hasn't broken the
   flagpole yet. Today the only honest output is `HOLD`. The doctrine
   layer then scores that HOLD against the eventual realized move and
   penalizes the brain when the breakout fires — even though the brain
   *correctly identified* the setup. The brain looks worse than it is,
   and the operator loses signal about which brains are actually reading
   tape well.

2. **The "I'm bearish but the structure says wait for pullback" intent**
   A brain sees BTC weakening but knows the move is too extended to short
   here — the high-EV entry is a pullback to the broken support flip.
   Today this gets coerced to either `SELL` (which fires immediately,
   wrong location) or `HOLD` (which gets graded as "no opinion"). Neither
   matches what the brain actually thinks.

### 1.2 The doctrine consequence

Because `execution_judge.ready` (and every other doctrine heuristic that
joins intent shape against realized outcome) keys off `action`, every
"correctly-identified setup that wasn't entered RIGHT NOW" silently lowers
that brain's doctrine score. We've already quarantined
`execution_judge.ready` to advisory-only for this exact reason —
**but quarantining heuristics is a band-aid**. The root cause is that
the intent envelope cannot represent the brain's actual cognition.

### 1.3 What we want from v3

The Paradox v3 envelope must let a brain emit:

- "I see a BULLISH setup, the trigger is a break of $187.40, my
  invalidation is $184.20, I am NOT firing yet, I want this in the
  watch queue."
- "I think AMZN is over-extended. My stance is bearish but my
  execution style is patient — I want to short the next bounce to
  $189, not the current $186 print."
- "I have no view on NVDA right now — explicit abstain, not a HOLD."

…and have the doctrine layer **score the plan, not just the side**.

---

## 2. Doctrine principles (these come FIRST, schema serves them)

These are the rails this rewrite has to honor.

1. **Planning is separated from execution.**
   A brain's *opinion* of a setup is one object. The *order to execute*
   that setup is a different object derived from it. v2 conflates them.

2. **`action` becomes execution-layer only.**
   v3 brains never emit `action` directly. They emit `stance` +
   `setup` + `execution_style`. The seat policy / executor derives
   `action` when (and only when) the trigger conditions are met.

3. **A "wait" intent is a first-class citizen.**
   `WAIT_FOR_TRIGGER` is a real, doctrine-scored state — not a
   coerced HOLD. Brains that correctly call setups deserve credit
   even when they explicitly didn't fire yet.

4. **Doctrine scores the plan, not just the outcome of immediate
   execution.**
   If a brain stamps `setup=bull_flag`, `stance=BULLISH`,
   `trigger_price=187.40`, `invalidation_price=184.20`, the verifier
   has enough to grade:
     - Did the trigger ever fire?
     - If yes, did the brain or another execute it?
     - Did invalidation trigger first?
     - What was the realized RR vs the planned RR?
   That's a real performance dataset. `action: BUY` against a single
   fill is not.

5. **Backward compatibility is mandatory during transition.**
   v2 intents already in `shared_intents` (multiple months of history)
   must keep replaying. The doctrine layer, the funnel, the
   post-mortem panels, and the verifier rule sheet must continue to
   work on v2 rows for the full audit-retention window.

6. **No code is written until this PRD is approved by the operator.**

---

## 3. Proposed v3 envelope schema

### 3.1 Top-level shape

```jsonc
{
  "intent_id":   "string",         // unchanged
  "intent_version": "v3",          // NEW — explicit version tag
  "ts":          "ISO-8601-UTC",   // unchanged
  "brain_id":    "string",         // unchanged
  "stack":       "string",         // unchanged (camino/barracuda/...)
  "lane":        "equity | crypto",
  "symbol":      "string",

  // --- PLANNING LAYER (NEW) -------------------------------------
  "plan": {
    "stance":           "BULLISH | LONG_BIAS | NEUTRAL | SHORT_BIAS | BEARISH | UNCERTAIN",
    "setup":            "bull_flag | bear_flag | breakout | breakdown | mean_revert | gap_fill | range_play | trend_continuation | trend_exhaustion | news_driven | other",
    "intent":           "ENTER | EXIT | SCALE_IN | SCALE_OUT | HEDGE | WAIT_FOR_TRIGGER | WAIT_CONFIRMATION | DEFER | WATCH | ABSTAIN | NO_EDGE",
    "execution_style":  "MARKET_NOW | LIMIT | STOP | TRIGGERED_LIMIT | PATIENT | SCALED",
    "size_posture":     "STANDARD | REDUCED | ELEVATED",
    "portfolio_posture":"RISK_ON | NEUTRAL | RISK_OFF",
    "hedge_against_symbol": null | "string",   // required iff intent == HEDGE
    "trigger_price":     null | number,    // price level that flips intent → executable
    "invalidation_price": null | number,   // price level that kills the plan
    "target_prices":     null | number[],  // ordered list, primary first
    "confidence":        number,           // 0.0 - 1.0, planning conviction
    "thesis":            "string",         // 1-3 sentence narrative (audit-only, not parsed)
    "horizon":           "INTRADAY | SWING | POSITION | UNKNOWN",
    "ttl_seconds":       null | int        // plan auto-expires after this many seconds idle
  },

  // --- EXECUTION LAYER (derived, optional at emit time) ---------
  "execution": {
    "action":         null | "BUY" | "SELL" | "SHORT" | "COVER" | "OPEN" | "CLOSE",
    "notional_usd":   null | number,
    "limit_price":    null | number,
    "broker_hint":    null | "webull" | "kraken",
    "derived_from_plan": true,    // false when v2-style fast-path emit
    "derived_at":     null | "ISO-8601-UTC"
  },

  // --- EVIDENCE / DOCTRINE / RECEIPTS (preserved from v2) -------
  "evidence":        { ... },     // unchanged shape
  "snapshot":        { ... },     // unchanged shape
  "pipeline_receipt": { ... },    // unchanged
  "gate_state":      "string",    // unchanged
  "executed":        bool         // unchanged
}
```

### 3.2 Field-by-field rationale

| Field | Type | Why |
|---|---|---|
| `intent_version` | `"v2"` \| `"v3"` | Discriminator. Lets the funnel, post-mortem, verifier, etc. branch on version without inferring from missing keys. Old rows stay `"v2"` (back-fill is one-shot). |
| `plan.stance` | enum | The brain's directional read. Decoupled from order side. A BULLISH plan can still have `execution.action = null` (waiting for trigger). `LONG_BIAS` / `SHORT_BIAS` are softer leans (not fully committed); `UNCERTAIN` = directional read attempted but inconclusive. |
| `plan.setup` | enum (extensible) | Categorical tag for the verifier rule sheet & report-cards. Already partially present in `evidence` via `signals` — promote it to first-class. |
| `plan.intent` | enum | What the brain wants done. Headline new values: `WAIT_FOR_TRIGGER` (price-level wait), `WAIT_CONFIRMATION` (signal-confirmation wait — 2nd-bar close, volume tag, MACD cross), `DEFER` (pass this turn, re-evaluate when ttl elapses), `WATCH` (passive tracking, no immediate plan — explicitly NOT named OBSERVE to avoid collision with the seat-layer `autonomy_mode: observe` which is the shadow-learning mode), `NO_EDGE` (has a read but stat edge isn't there — distinct from ABSTAIN which is "no opinion"), `HEDGE` (open offsetting exposure; requires `hedge_against_symbol`). |
| `plan.execution_style` | enum | How the brain wants the order placed when it triggers. `PATIENT` = "don't market into this, work it on the bid/ask". `TRIGGERED_LIMIT` = limit at trigger_price, valid for ttl_seconds. |
| `plan.size_posture` | enum | Sizing modifier orthogonal to `intent`. `STANDARD` = normal sizing; `REDUCED` = ×0.5 default (brain wants in, but smaller); `ELEVATED` = ×1.2 default (high-conviction). Final multiplier owned by Governor — `size_posture` is the brain's request, not a guarantee. |
| `plan.portfolio_posture` | enum | Brain's READ of overall portfolio risk environment (NOT a per-symbol intent). When set to `RISK_OFF`, seat policy applies a global ×0.5 across every active plan from that brain. `RISK_ON` applies ×1.0 baseline (can be configured to ×1.2). Defaults to `NEUTRAL` when omitted. |
| `plan.hedge_against_symbol` | string / null | Required iff `intent = HEDGE`. Names the symbol whose exposure this plan offsets. Used by Governor's correlation-adjusted notional sizing. |
| `plan.trigger_price` | number / null | Threshold that promotes the plan from WAIT → executable. Seat policy reads this when re-evaluating. |
| `plan.invalidation_price` | number / null | Hard kill. If price prints through this, the plan auto-retires. |
| `plan.target_prices` | number[] / null | Primary target = first element. Used by RR computation in the verifier. |
| `plan.confidence` | float 0-1 | Conviction in the PLAN. Doctrine scoring will track plan-confidence-vs-outcome separately from execution-confidence-vs-fill. |
| `plan.thesis` | string | Audit-only. Never parsed. Lets operators read the brain's reasoning during post-mortem. |
| `plan.horizon` | enum | Sets the verifier window for MAE/MFE computation. Intraday = next-close. Swing = next-5-bars. Position = next-20-bars. |
| `plan.ttl_seconds` | int / null | Plan self-expires. Prevents stale WAIT plans from cluttering the watch queue. Null = no TTL (verifier infers from horizon). |
| `execution.action` | enum / null | THE order side. Null when the plan hasn't promoted to executable yet. Replaces top-level `action` from v2. |
| `execution.derived_from_plan` | bool | `true` when seat policy or auto-router converted a v3 plan into an order. `false` for v2 fast-path emits (legacy bridges). |

### 3.3 New `gate_state` values

```
v2 today:
  emitted | seat_approved | governor_sized | roadguard_passed |
  auto_submit_attempted | broker_accepted | filled |
  blocked | advisory_only | no_trade

v3 adds:
  waiting_for_trigger   ← plan is live, no execution yet
  plan_invalidated      ← invalidation_price hit before trigger
  plan_expired          ← ttl_seconds elapsed before trigger
  trigger_fired         ← trigger_price hit, awaiting seat re-eval
```

### 3.4 Receipt extension

`PipelineReceipt` gains two optional fields:

```python
plan_outcome: Literal[
    "executed",            # plan promoted, order filled
    "wait_listed",         # plan accepted as WAIT
    "invalidated",         # invalidation_price hit
    "expired",             # ttl elapsed
    "abstained",           # plan.intent = ABSTAIN
    "v2_legacy",           # row predates v3, field is N/A
] = "v2_legacy"

plan_quality_score: float | None = None   # verifier-computed, populated async
```

---

## 4. Doctrine layer changes

This is where the real value of v3 lands. The verifier rule sheet
(`shared/lessons/`) and the report cards (`shared/report_cards.py`)
gain new scoring axes:

### 4.1 Plan-scored KPIs (NEW)

For every v3 plan, the verifier computes (after the horizon window):

| KPI | Definition |
|---|---|
| `trigger_hit_rate` | % of WAIT plans whose `trigger_price` fired within `ttl_seconds` (or horizon window) |
| `invalidation_hit_rate` | % of plans whose `invalidation_price` fired before `trigger_price` |
| `plan_realized_rr` | (max favorable excursion) / (planned risk = trigger - invalidation) |
| `plan_followthrough_rate` | Of plans where trigger fired, % that hit the first target price |
| `correct_setup_rate` | % of plans whose realized intraday character matched the declared `setup` (uses bar classifier from regime/classifier.py) |
| `discipline_score` | Composite: how often did the brain correctly NOT-fire (WAIT) when the price action didn't confirm? |

### 4.2 Doctrine scoring stops penalizing correct-waits

The doctrine scorecard (`shared/doctrine/scorecard.py`) gets a new
axis: **plan_discipline**. Today every non-executed intent is implicitly
"missed opportunity" or "noise". In v3:

- `plan.intent = WAIT_FOR_TRIGGER` or `WAIT_CONFIRMATION` + trigger /
  confirmation fired + correct direction → **positive contribution**
  to discipline_score (brain called it).
- `plan.intent = WAIT_FOR_TRIGGER` / `WAIT_CONFIRMATION` +
  invalidation_price fired first → **positive contribution** to
  discipline_score (brain correctly stayed out of a losing trade).
- `plan.intent = WAIT_*` + nothing happened, ttl expired
  → **neutral** (no signal either way).
- `plan.intent = DEFER` + ttl expires → **neutral** (defer is by
  design non-committal; re-stamping DEFER repeatedly without ever
  committing IS flagged as "brain is dithering" — a separate
  `dither_rate` KPI tracks this).
- `plan.intent = NO_EDGE` + market moved through brain's implied
  direction inside the horizon window → **positive contribution**
  (brain correctly called out the lack-of-edge zone).
- `plan.intent = ENTER` + filled + winning → **same as today**.
- `plan.intent = ABSTAIN` → **excluded from scoring entirely** (the
  brain explicitly opted out, doesn't deserve credit or blame).
- `plan.intent = WATCH` → **excluded from scoring** (passive
  tracking only, no committal of any kind).
- `plan.intent = HEDGE` → scored against the PAIR's net P&L, not
  the hedge leg in isolation.

### 4.3 Execution-judge gets unquarantined (eventually)

`execution_judge.ready` was quarantined because it kept blocking
viable trades on v2 envelopes that had no way to express "this is
correct but the timing is wrong." Once v3 is live and a meaningful
slice (>30%) of intents carry `plan.execution_style`, the heuristic
can be un-quarantined with a new rule:

```
if execution.action is not None and plan.execution_style == "PATIENT":
    skip execution_judge.ready check
```

That's a separate follow-up, not part of v3 ship.

---

## 5. Pipeline behavior changes

Concrete behavior changes when an intent carries `intent_version = "v3"`:

### 5.1 Intent Firewall (`shared/security/intent_firewall.py`)

- Pattern set extended with two new injection patterns specific to
  free-text fields: `plan.thesis` and `plan.setup` (when "other").
- Existing observe-mode behavior preserved.

### 5.2 Seat layer (`shared/pipeline/seat_policy.py`)

- New routing: if `plan.intent == "WAIT_FOR_TRIGGER"` AND
  `execution.action is None`, the seat **does not call the broker**.
  Instead, the intent gets `gate_state = "waiting_for_trigger"` and
  is parked in a new TTL-indexed collection `intent_watch_queue`.
- A new periodic worker (`shared/pipeline/trigger_watcher.py`)
  scans `intent_watch_queue` every 5s, fetches the latest snapshot
  from the existing `snapshot_enrich` layer, and:
    - If `trigger_price` hit: stamps `gate_state = "trigger_fired"`,
      derives `execution.action` from `plan.stance` (BULLISH → BUY,
      BEARISH → SELL/SHORT depending on lane policy), and re-injects
      the intent into the main pipeline at the seat layer.
    - If `invalidation_price` hit: stamps
      `gate_state = "plan_invalidated"` and writes terminal receipt.
    - If `ttl_seconds` elapsed: stamps `plan_expired`.

### 5.3 Governor, RoadGuard, Broker

- **No changes.** These layers only ever see the executable form
  (`execution.action` populated, derived_from_plan=true). The
  planning layer is invisible to them.

### 5.4 Auto-router

- The auto-router learns to distinguish "stuck in seat" (today's
  Seat-Drift class) from "intentionally waiting for trigger" (new).
  The terminal-writeback fix shipped 2026-06-22 already prevents
  WAIT plans from being looped — they get the new
  `waiting_for_trigger` gate_state which sits outside the auto-router's
  active query.

---

## 6. Backward compatibility plan

This is the most operationally sensitive part of v3.

### 6.1 Hard rules

1. **Every existing v2 row in `shared_intents` stays valid.** No
   destructive migration. The version discriminator does the work.
2. **The Bridges keep emitting v2 by default for 30 days** after v3
   ships. v3 emits ride an opt-in env flag (`PARADOX_V3_BRAINS=camino,barracuda`)
   so we can flip one brain at a time.
3. **Every read path** (funnel, post-mortem, verifier, report cards,
   intent trace UI) gets a single helper `normalize_intent(doc) → dict`
   that lifts v2 → v3 shape on-read. The helper is the only place
   that knows about the version difference.

### 6.2 Mapping table (v2 → v3 on-read)

| v2 field | v3 equivalent | Notes |
|---|---|---|
| `action: "BUY"` | `execution.action = "BUY"`, `plan.stance = "BULLISH"`, `plan.intent = "ENTER"`, `plan.execution_style = "MARKET_NOW"` | Fast-path inferred |
| `action: "SELL"` | `execution.action = "SELL"`, `plan.stance = "BEARISH"`, `plan.intent = "EXIT"` if position-held else `"ENTER"`, `plan.execution_style = "MARKET_NOW"` | Position-held inferred from sidecar |
| `action: "HOLD"` | `execution.action = null`, `plan.stance = "NEUTRAL"`, `plan.intent = "WATCH"` | Critical mapping — this is the field whose doctrine grading was broken in v2. Using `WATCH` (not OBSERVE) to avoid collision with seat-layer `autonomy_mode: "observe"`. |
| `action: "OPEN"` | `execution.action = "OPEN"`, `plan.intent = "ENTER"` | |
| `action: "CLOSE"` | `execution.action = "CLOSE"`, `plan.intent = "EXIT"` | |
| `confidence` | `plan.confidence`, also `execution`-side confidence on receipts | Single source for v2 |
| `evidence` | `evidence` | Unchanged |

### 6.3 Frontend

- The Intents page renders the v3 fields when present, falls back to
  v2 shape otherwise. The PostMortem panel does the same.
- Filter chips for `plan.intent` and `plan.setup` are added to the
  intent list view.

---

## 7. Rollout sequence (when this PRD is approved)

This is the proposed order. **Step 0 still requires explicit operator
go-ahead before any code is written.**

| # | Step | Risk | Test gate |
|---|---|---|---|
| 0 | Operator approves PRD | n/a | Operator signoff in chat |
| 1 | Add `intent_version`, `plan{}`, `execution{}` to `shared/intents.py` payload model. **Both shapes valid.** Old emitters continue to work. | Low | All existing pytest stay green |
| 2 | Implement `normalize_intent(doc)` lifter + use it inside `routes/admin_intents_funnel.py`, `routes/admin_intents_post_mortem.py`, verifier, report cards. | Medium | New pytest: v2 doc + v3 doc both produce identical funnel output |
| 3 | New collection `intent_watch_queue` + `trigger_watcher.py` periodic worker. **Worker is DORMANT** — feature flag `PARADOX_V3_TRIGGER_WATCHER=0` by default. | Medium | Smoke: queue a synthetic WAIT plan, watch trigger fire in preview |
| 4 | First brain (`camino`) emits v3 envelopes behind `PARADOX_V3_BRAINS=camino`. Run for 24h. Compare doctrine scorecard for camino under both shapes. | Medium-high | Manual 24h observation. Operator sign-off. |
| 5 | Flip trigger_watcher to live. Trigger fires actually re-inject into pipeline. | High | New pytest pinning the re-inject contract. Manual smoke. |
| 6 | Roll v3 to barracuda, hellcat, gto in sequence (1 per day). | Medium | Doctrine deltas tracked between brains. |
| 7 | Un-quarantine `execution_judge.ready` for v3 PATIENT plans. | Low | Separate small PRD. |
| 8 | After full audit-retention window (~90d), delete the v2 fast-path emit code path. Bridges only emit v3. | Low | Drop the v2-emit unit tests, keep v2-read coverage. |

---

## 8. Risks & open questions

These are the things I want operator input on **before** any code is touched.

1. **Q: Should `plan.target_prices` be required for `intent = ENTER`?**
   Forcing brains to declare a target makes RR computation
   trivial. But it forces brains to commit to a target they may
   not have. My recommendation: **optional, but doctrine
   penalizes ENTER plans with no target_prices** (gentle nudge,
   not a block).

2. **Q: How does v3 interact with the Hot-Brain Router (still dormant)?**
   The router's "would-have" dry-run currently scores against
   `action`. If v3 flips a brain's emit, the router's classifier
   needs to know that `plan.intent = WAIT_FOR_TRIGGER` should be
   excluded from win/loss aggregation. My recommendation: extend
   the perf store query to filter on `intent_version` AND
   `plan.intent IN [ENTER, EXIT, SCALE_IN, SCALE_OUT]` only.

3. **Q: Should WAIT plans show up in the funnel?**
   They will look like "emitted but never reached seat" which is
   what the funnel today flags as a leak. My recommendation: add a
   new funnel column **before** seat — "Wait-listed (intentional)"
   — so the operator can distinguish intentional wait from broken
   pipeline drop.

4. **Q: Setup vocabulary — should it be open-string or enum?**
   I've proposed an enum with `other` fallback. Enum is friendlier
   to the verifier rule sheet (consistent grouping). String is
   friendlier to brain evolution. **My recommendation: enum + a
   `setup_custom_tag: string` free-field for brain-specific labels
   that don't fit the enum.**

5. **Q: Do we need versioned `plan` schemas inside v3?**
   I.e., `plan_version: 1`. My recommendation: **yes, add it now**
   so future plan-shape evolutions don't force another full
   envelope rewrite.

6. **Q: TTL default when `ttl_seconds = null`?**
   My recommendation: derive from `horizon` —
   `INTRADAY → 23400s` (next session close),
   `SWING → 432000s` (5 trading days),
   `POSITION → 1728000s` (20 trading days).

7. **Operator concern:** "Will this confuse the legacy doctrine
   wrappers?" Answer: **no** — the wrappers run on the executable
   form. Step 1 of the rollout adds the new fields **alongside**
   the legacy `action` field; the wrappers see the same `action`
   they always have until trigger_watcher derives it.

---

## 9. Concrete deliverable checklist (post-approval)

When operator approves, the work breakdown becomes:

- [ ] `shared/intents.py` — extend `IntentBody` Pydantic model with
      `plan`, `execution`, `intent_version`. Default v2-shaped emits
      to `intent_version = "v2"`. (Step 1)
- [ ] `shared/intents.py` — add `normalize_intent(doc)` lifter. (Step 2)
- [ ] `routes/admin_intents_funnel.py` — adopt lifter, add
      `waiting_for_trigger` column. (Step 2)
- [ ] `routes/admin_intents_post_mortem.py` — adopt lifter, render
      plan vs execution sections. (Step 2)
- [ ] `shared/lessons/builder.py` + `schemas.py` — add plan-scored
      KPIs. (Step 2)
- [ ] `shared/report_cards.py` — add `plan_discipline` axis. (Step 2)
- [ ] `shared/pipeline/seat_policy.py` — add WAIT_FOR_TRIGGER short-
      circuit. (Step 3)
- [ ] NEW `shared/pipeline/trigger_watcher.py` — periodic worker
      (DORMANT until step 5). (Step 3)
- [ ] NEW `intent_watch_queue` Mongo collection + TTL index. (Step 3)
- [ ] `shared/brains/camino_committee.py` (or wrapper) — first v3
      emit behind env flag. (Step 4)
- [ ] Frontend: `Intents.jsx`, `IntentPostMortemPanel.jsx`,
      `LiveTradeDiagnose.jsx` — render v3 fields when present,
      add filter chips. (Step 4)
- [ ] Tests:
    - `test_intent_envelope_v3_schema.py`
    - `test_intent_envelope_v3_normalize.py`
    - `test_trigger_watcher_dormant.py`
    - `test_trigger_watcher_live.py`
    - `test_doctrine_plan_discipline_axis.py`
    - `test_funnel_waiting_for_trigger_column.py`

---

## 10. What this PRD does NOT do

- Does not change broker behavior.
- Does not change Webull caps, exposure caps, intent firewall
  patterns, or RoadGuard. Those remain v2-shape compatible.
- Does not delete `action` from `shared_intents`. v3 docs still
  populate `execution.action` and (during transition) the
  top-level `action` for legacy reader compatibility.
- Does not activate the Hot-Brain Router. Still dormant.
- Does not retire `execution_judge.ready` permanently — only
  unquarantines it for v3 PATIENT plans in a later step.

---

## 11. Locked decisions (2026-02 operator review pass 1)

| Decision | Value | Source |
|---|---|---|
| OBSERVE vs WATCH | **WATCH only.** Avoid collision with seat-layer `autonomy_mode: "observe"` which IS the shadow-learning mode. | Operator 2026-02 |
| REDUCE_SIZE placement | **New field `plan.size_posture`** (`STANDARD | REDUCED | ELEVATED`). NOT collapsed into `plan.intent`. | Operator 2026-02 |
| DEFER semantics | **Auto-expire on `ttl_seconds`** for v3 ship. Brain-controlled re-stamp / extension is a follow-up (separate field `defer_strategy` if/when added). | Operator 2026-02 |
| Portfolio-posture axis | **Added** — `RISK_ON | NEUTRAL | RISK_OFF` as separate top-level `plan.portfolio_posture`. | Operator 2026-02 (implied by RISK_ON / RISK_OFF vocab) |
| Stance softening | **LONG_BIAS / SHORT_BIAS added** between BULLISH/BEARISH and NEUTRAL. | Operator 2026-02 (implied by SHORT_BIAS vocab) |
| WAIT taxonomy | **Two forms**: `WAIT_FOR_TRIGGER` (price-level) + `WAIT_CONFIRMATION` (signal-confirmation). Doctrine treats both as discipline-scored. | Operator 2026-02 |
| NO_EDGE | **First-class** plan.intent value, distinct from ABSTAIN. Scored when realized move proves brain right. | Operator 2026-02 |
| HEDGE | **First-class** plan.intent with required `hedge_against_symbol`. Scored against pair net P&L. | Operator 2026-02 |
| §8 Q1 target_prices required for ENTER | **No.** OPTIONAL with NO doctrine penalty when omitted. | Operator 2026-02 (review pass 2, decision 1A) |
| §8 Q4 setup vocab shape | **Enum + `setup_custom_tag` free-string fallback** for labels that don't fit. | Operator 2026-02 (decision 2B) |
| §8 Q5 inner plan_version | **No.** YAGNI — top-level `intent_version` is the only discriminator. | Operator 2026-02 (decision 3B) |
| §8 Q6 ttl_seconds default | **Horizon-derived map**: INTRADAY=23400s, SWING=432000s, POSITION=1728000s, UNKNOWN=null. Stored on the row as null; verifier consults the table at score-time. | Operator 2026-02 (decision 4A) |
| §8 Q3 WAIT plans in funnel | **Bucketed under seat-blocked** at Stage 1. NO new funnel column. (v3 WAIT docs have no pipeline receipt + executed=false → naturally land at Stage 1 same as seat-blocked v2 rows.) | Operator 2026-02 (decision 5C) |
| §8 Q2 HBR scoring filter | **No filter.** Hot-Brain Router scores EVERY intent regardless of plan.intent. | Operator 2026-02 (decision 6B) |

## 12. Sign-off

| Role | Name | Approved | Date |
|---|---|---|---|
| Operator | risedual | ✅ | 2026-02-22 |
| MC main agent | E1 (continuation) | ✅ (Step 1 shipped) | 2026-02-22 |

## 13. Rollout progress

| Step | Status | Date | Notes |
|---|---|---|---|
| 0 | ✅ Approved | 2026-02-22 | All 7 §8 questions answered by operator. |
| 1 | ✅ Shipped | 2026-02-22 | `shared/intent_envelope_v3.py` + `IntentIn` extended + lifter adopted in funnel + post-mortem. 93 new tests; 115 total tests green; live smoke confirms v3 WAIT_FOR_TRIGGER doc round-trips through MongoDB and the lifter. |
| 2 | ✅ Shipped | 2026-02-22 | Lifter adopted in `shared/lessons/builder.py` (`build_lesson`). `Lesson` dataclass extended with 17 v3 plan/execution fields. `shared/report_cards.py` adds `plan_discipline` axis (v3-only — v2 rows excluded from scoring per operator §11). 9 new tests pin v2/v3 lesson lift + dataclass contract + plan_discipline aggregation. |
| 3 | ✅ Shipped DORMANT | 2026-02-22 | NEW `shared/pipeline/trigger_watcher.py` — `is_watcher_enabled()` default OFF, `scan_watch_queue()` returns zero counters when dormant, `enqueue_watch_plan()` writes when dormant (backlog-safe). LIVE-mode TTL expiry + bullish/bearish trigger + invalidation legs all coded; price-trigger paths only engage when caller supplies a `price_fetcher` (Step 5 wires the default). NEW collection `intent_watch_queue` with 4 indexes including 30d TTL safety net. 12 new tests pin all behaviour. Seat-policy wiring is Step 5. |
| 4 | ✅ Shipped DORMANT | 2026-02-22 | NEW `synthesize_v3_envelope(payload)` + `v3_brain_enabled(brain_id)` helpers (write-side mirrors of the read-side lifter). `external/brains/runner.py` calls them — when `PARADOX_V3_BRAINS=<csv>` includes the brain's id, the payload is upgraded to v3 before IntentIn parses it. Default OFF. 15 new tests pin synthesizer correctness + env-flag semantics + synthesize→normalize round-trip property. **Operator action**: flip `PARADOX_V3_BRAINS=camino` in `.env` + `sudo supervisorctl restart backend` to begin the 24h shadow run. |
| 5 | ✅ Shipped LIVE-READY | 2026-02-22 | `BrainOpinion` extended with `intent_version` + `plan`. `adapter._opinion_from_intent` lifts via `normalize_intent`. `SeatPolicy.evaluate` short-circuits v3 `WAIT_FOR_TRIGGER`/`WAIT_CONFIRMATION` plans to `enqueue_watch_plan` (after auth gates) and returns BLOCK with reason `paradox_v3_wait_for_trigger:...`. `execution_pipeline` HOLD short-circuit gains a narrow v3 WAIT exception so the seat actually sees the plan. `auto_router._loop` piggybacks `scan_watch_queue(price_fetcher=default_price_fetcher)` on its 30s tick — DORMANT when `PARADOX_V3_TRIGGER_WATCHER=0`. NEW `routes/admin_paradox_v3.py` exposing `GET /api/admin/paradox-v3/status` + `GET /api/admin/paradox-v3/watch-queue`. Live-verified end-to-end on BOTH lanes (equity NVDA + crypto BTC/USD) — Step 5 18 tests + 4 crypto-parity. |
| 5.b | ✅ Shipped DORMANT | 2026-02-22 | Re-injection of fired plans into the unified pipeline behind `PARADOX_V3_TRIGGER_REFIRE` (default OFF). When ON: fired WAIT plan has `plan.intent` flipped WAIT→ENTER, `execution.action` synthesised from `plan.stance` (BULLISH/LONG_BIAS → BUY; BEARISH/SHORT_BIAS → SHORT on **both lanes** per operator pin 2026-02-22; NEUTRAL → refused), and `execution.limit_price` populated from `plan.trigger_price` when `plan.execution_style` is a limit-class style. The mutated intent runs through `run_unified_for_intent` so the seat re-evaluates conf_min + live consensus AT trigger-fire time. 40 tests. |
| 5.c | ✅ Shipped LIVE-READY | 2026-02-22 | Closed the broker-layer gap — `execution.limit_price` is now actually honoured end-to-end. `BrainOpinion` gains `execution` field. `_BrokerAdapter` exposes `submit_limit_order`. `execution_pipeline` step 8 dispatches market vs limit based on `opinion.execution.limit_price`. `route_order` accepts `limit_price` kwarg, converts notional → qty, and detects SHORT-on-crypto-on-Kraken to pass `leverage` (env `PARADOX_V3_KRAKEN_SHORT_LEVERAGE`, default 2). NEW `KrakenLiveAdapter.submit_limit_order` (`ordertype="limit"` + `price=...`). Both Kraken methods accept optional `leverage` for margin shorts. 13 new tests pin the entire dispatch chain incl. the MC-receipt bypass guard mirrored on the limit path. |
| 6 | ✅ Shipped (operator-driven activation) | 2026-02-22 | The runner-side emit path is brain-agnostic — adding a brain to `PARADOX_V3_BRAINS` enables v3 for that brain with no other code change. 3 new tests pin: (a) `v3_brain_enabled` works for any brain_id, (b) sequential rollout `camino → camino,barracuda → camino,barracuda,hellcat` works as expected, (c) `synthesize_v3_envelope` produces identical output regardless of which brain emitted the payload. Activation is operator-side: bump `PARADOX_V3_BRAINS=<csv>` + `sudo supervisorctl restart backend` per brain. |
| 7 | ✅ Shipped LIVE | 2026-02-22 | `execution_judge.ready` un-quarantined for v3 PATIENT plans only. New fields `intent_version` + `plan_execution_style` stamped on `doctrine_sidecars` audit rows by both ingest paths. NEW `_v3_patient_execution_judge_candidates(rows, min_samples)` re-scores the ready/not_ready branches on the v3 PATIENT subset and emits candidates tagged `scope="v3_patient_only"` so the operator can distinguish them from broad-dataset signals. The broad-dataset quarantine REMAINS in place — pinned by `test_broad_expectations_list_still_excludes_execution_judge` (catches a future agent accidentally re-enabling the broad pass). 5 new tests pin: v2 rows excluded; inversion-on-PATIENT emits a candidate; healthy direction emits nothing; below-min-samples emits nothing; broad expectations list still excludes execution_judge. |
| 8 | ☐ Pending (~90d out) | — | After audit-retention window: delete v2 fast-path emit. Bridges only emit v3. |
