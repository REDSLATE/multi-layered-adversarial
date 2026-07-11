# MC Seat Arbiter — Design Freeze

**Status**: v0.1 — pending operator sign-off
**Author**: 2026-07-11 handoff session
**Doctrine anchors**: Bruce Lee ("remove what is not needed") · Live-only or DISARMED (no shadow arm) · One truth doc per stack

---

## 1. The shift

MC stops being a passive log. MC becomes the **seat arbiter**:

```
Brain (Camino)     Brain (Barracuda)     Brain (Hellcat)     Brain (GTO)
     │                    │                    │                  │
     └────── opinion ─────┴────── opinion ─────┴───── opinion ────┘
                                    │
                                    ▼
                      ┌──────────────────────────┐
                      │   MC SEAT ARBITER        │
                      │  ─────────────────────   │
                      │  · Collect opinions      │
                      │  · Apply DAWE weight     │
                      │  · Rank + pick winner    │
                      │  · Size (kernel × √w)    │
                      │  · Emit ONE intent       │
                      │  · Grade ALL opinions    │
                      │  · Update DAWE           │
                      └──────────────────────────┘
                                    │
                                    ▼
                                 Trader
                                    │
                                    ▼
                                 Broker
```

Brains become **opinion producers**. MC owns:
- Direction competition (rank + winner)
- Sizing (single implementation, no per-brain duplicates)
- DAWE weight state per (seat, brain)
- Confidence floor / gate policy (single implementation)
- Grading + learning loop

Brains lose:
- Sizing math
- Confidence floors / RVOL gates / hard vetoes
- Direct submission to `shared_intents`

---

## 2. Seat key

```
seat_key = f"{lane}:{symbol}:{bucket_iso}"

# Where:
#   lane        = "equity" | "crypto"
#   symbol      = uppercase ticker ("NVDA", "ETH/USD")
#   bucket_iso  = current UTC time floored to 5-minute boundary,
#                 ISO-8601, no microseconds
#                 e.g. "2026-07-11T14:30:00Z"
```

**5-min bucket rationale**: bounds cardinality (~288 seat rows/symbol/day), matches the 15m grade horizon, gives brains ~5 min to submit opinions before the seat is arbitrated.

---

## 3. Opinion shape

Emitted by each brain to MC (replaces the current direct-to-shared_intents post):

```python
@dataclass(frozen=True)
class ModelOpinion:
    brain: str                    # "camino" | "barracuda" | "hellcat" | "gto"
    seat_key: str                 # see §2
    direction: Direction          # LONG | SHORT | FLAT
    edge: float                   # 0.0-1.0, expected R:R normalized
    confidence: float             # 0.0-1.0, brain's certainty
    regime_fit: float             # 0.4-1.0, brain's regime alignment
    urgency: float                # 0.0-1.0, time-decay of the opportunity
    price_at_signal: float        # for grader
    entry_hint: float | None      # optional preferred entry
    stop_hint: float | None       # optional preferred stop
    rationale: str                # short human string
    ts: str                       # ISO-8601 UTC, brain's emission time

    @property
    def rank_score(self) -> float:
        return (
            self.edge
            * self.confidence
            * self.regime_fit
            * (0.75 + self.urgency * 0.25)
        )
```

**Direction=FLAT** is a legitimate opinion (brain looked and passed). It's graded but never wins a seat.

---

## 4. DAWE weight math

State per (brain, lane) — NOT per (brain, symbol) at v0.1. Escalate to (brain, symbol) only if per-symbol dispersion proves meaningful.

```python
@dataclass
class DaweState:
    brain: str
    lane: str

    # Bounded [0.4, 1.4]. Never zero — no permanent exile.
    session_weight: float = 1.0    # last 90 min, EWMA α=0.30
    recent_weight: float = 1.0     # last 5-10 sessions, EWMA α=0.10
    prior_weight: float = 1.0      # long-run baseline, cold-refreshed nightly

    grades_used_session: int = 0
    grades_used_recent: int = 0
    last_updated: str = ""

    @property
    def effective_weight(self) -> float:
        raw = (
            self.session_weight ** 0.50
            * self.recent_weight ** 0.30
            * self.prior_weight ** 0.20
        )
        return max(0.40, min(1.40, raw))
```

**Exponents rationale**: 50/30/20 (not 40/25/20/15) at v0.1 because we're dropping the "session context" primitive (see §7). Add it later if the graded-outcome signal proves noisy.

**Cold start**: any DAWE state with `grades_used_session < 5` returns `effective_weight = 1.0` regardless of computed value. Prevents thin-data noise from moving the arm.

---

## 5. Arbitration loop

Runs at each seat closure (5-min bucket boundary):

```
1. Collect all opinions posted to seat_key in the last 5 min.
2. For each opinion, compute:
     adjusted_rank = opinion.rank_score * dawe[brain, lane].effective_weight
3. Winner:
     If any LONG or SHORT opinions exist:
       winner = argmax(adjusted_rank) among directional opinions
     Else:
       No trade this seat. All opinions still graded (§6).
4. Compute disagreement multiplier from the opposition field:
     opposition_strength = max rank of opinions with opposing direction
     disagreement_mult = clamp(1.0 - opposition_strength * 0.40, 0.55, 1.00)
5. Size:
     base_risk = account_equity * config.risk_fraction
     size_mult = kernel_multiplier * disagreement_mult * sqrt(effective_weight)
     size_mult = clamp(size_mult, 0.30, 2.00)  # sanity gate
     target_risk = base_risk * size_mult
6. Emit ONE intent to shared_intents (MC's write) → trader picks up.
7. Record seat doc with opinions + winner + size + intent_id.
```

**Kill switch / master switch**: still hard-block before step 6. Mechanical invalidity only — doctrine unchanged.

---

## 6. Grader

Background task, cadence 60s:

```
For each opinion aged ≥15 min without a 15m grade:
    price_now = latest bar close for opinion.symbol
    signed_return = (price_now / opinion.price_at_signal - 1) * (+1 if LONG else -1 if SHORT else 0)
    expected_move = 1.0 * atr_at_signal / price_at_signal   # rough scale
    quality_15m = clamp(0.5 + signed_return / expected_move, 0.0, 1.0)
    write grade to opinion doc.

Same at 60m for opinions aged ≥60 min without a 60m grade.

After grade is written:
    For each (brain, lane), compute observed_session_fit as EWMA of recent 15m grades.
    Update dawe[brain, lane].session_weight = ewma(prev, observed, α=0.30)
    Update dawe[brain, lane].recent_weight = ewma(prev, session_avg, α=0.10)  # daily
```

FLAT opinions grade as "avoided harm if realized_return was small, avoided upside if realized_return was large in EITHER direction". Slightly different formula, same shape.

**This is grading of real predictions against real market moves. It is NOT shadow trading.**

---

## 7. What's explicitly OUT of scope at v0.1

Because we're de-risking a large design:
- **SessionContext primitives** (trend_strength, breadth, correlation, news_intensity). Add later if the graded-outcome signal alone proves noisy.
- **Regime-change detector with hysteresis**. Static EWMA α is good enough at v0.1.
- **Per-symbol DAWE state**. Start at (brain, lane).
- **Historical prior warmup**. `prior_weight` starts at 1.0 for everyone; nightly cold-refresh added later.

These are all Phase 2. Ship v0.1, measure for 5 sessions, THEN decide what to add.

---

## 8. What gets deleted

Per Bruce Lee doctrine, once seat arbiter is live and armed:
- Brain-side confidence floors (`MIN_CONFIDENCE` per brain)
- Brain-side RVOL gates
- Brain-side sizing math
- Brain-side direct calls to `shared_intents.insert_one` (goes through MC now)
- Any "would-have-executed" simulator or shadow-arm arbiter

**Kept**:
- Kernel Review, RISE AI, memory_kernel, counterfactual_signals (learning surfaces on the real tape)
- Kill switch, master switch, mechanical validators (physical valve)
- 3-clock write health, Atlas timeout handler (infra)

---

## 9. Runtime modes

Only two:
- **DISARMED** (default): opinions collected, arbitration runs, DAWE grades update, NO intent emitted to trader.
- **LIVE**: same, but intent IS emitted. Kill switch and mechanical validators still gate the emission.

**No PAPER. No SHADOW. No "would-have-picked" side channel.**

Flipping DISARMED→LIVE is the operator's single decision. Everything else is doctrine-driven.

---

## 10. Data locations

- **Opinions + seats**: new collection `mc_seats` (compound index `(seat_key, brain)`, TTL 30 days).
- **DAWE state**: extend `brain_runtime_metrics.risedual_stack` doc:
  ```
  brains.<brain>.dawe.<lane> = {session, recent, prior, effective, grades_used_session, grades_used_recent, last_updated}
  ```
  One doc read gives the whole matrix.
- **Grader progress**: field on each opinion doc; no separate collection.

---

## 11. Directory layout

```
/app/backend/mc_arbiter/
    __init__.py
    models.py           # ModelOpinion, SeatKey, DaweState (frozen dataclasses)
    seat_key.py         # bucket_iso helper, canonicalization
    dawe.py             # weight math + EWMA helpers
    arbiter.py          # arbitration loop, winner selection, sizing
    grader.py           # background loop
    routes.py           # POST /api/mc/arbiter/opinion
    tests/              # unit tests colocated
```

Deletions (Phase 3 of ship): brain-side gates + duplicate sizing. Tracked separately.

---

## 12. Sign-off checkboxes

- [x] Design shape approved
- [x] Seat key format `{lane}:{symbol}:{5min_bucket_iso}` confirmed
- [x] DAWE state at (brain, lane) — not (brain, symbol) — at v0.1 confirmed
- [x] SessionContext primitives deferred to Phase 2 confirmed
- [x] Runtime modes = { DISARMED, LIVE } only (no shadow/paper) confirmed
- [x] Deletion list §8 approved

**Signed off**: 2026-07-11 by operator (in-chat approval).
