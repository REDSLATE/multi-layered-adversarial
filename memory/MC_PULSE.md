# MC Pulse — Design Freeze

**Status**: v0.1 — pending operator sign-off
**Companion to**: `MC_SEAT_ARBITER.md` (which owns arbitration/DAWE/grading math)
**Doctrine anchor**: "MC owns time, data, scheduling, arbitration, persistence, and execution routing. Brains own only interpretation."

---

## 1. The shift

Runners were never architectural boundaries — they were duplicated orchestration wrapped around four strategy calls. Collapse them into a single MC pulse. Brains become Python objects with one method: `evaluate(snapshot) -> ModelOpinion | None`.

**MC owns**:
- Clock / cadence
- Snapshot construction (single source of market truth per tick)
- Symbol universe / roster
- Persistence + idempotency
- Timeouts / containment
- Arbitration
- Execution routing
- Heartbeat (pulse receipt IS the heartbeat)

**Each brain owns**:
- Feature selection (which parts of the snapshot it looks at)
- Signal interpretation
- Confidence formation
- Contrarian behavior / thresholds
- Time horizon
- Memory / rolling state (namespaced by brain + strategy version)
- Objection vocabulary (`reason_codes`)
- Learning history

**MC must NOT standardize interpretation.** Reducing four brains to `personality_multiplier[brain_id] * common_strategy(snapshot)` destroys them.

---

## 2. Immutable snapshot

Built once per pulse; each brain sees the same immutable view. Contamination between brains is a silent-bug factory.

```python
@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    lane: str                             # "equity" | "crypto"
    timestamp: datetime                   # aware, UTC
    price: Decimal
    indicators: Mapping[str, float]       # atr, rvol, ema20, etc.
    market_state: MarketState             # trending / ranging / vol-expansion / ...
    snapshot_id: str                      # uuid4 hex; carries into envelopes
```

Brain-local rolling state stays on the brain instance. Anything representing *shared market truth* is immutable.

---

## 3. Brain protocol

```python
class Brain(Protocol):
    id: str
    lanes: frozenset[str]                 # {"equity"} or {"crypto"} or both
    cadence_seconds: int
    evaluation_timeout_seconds: float

    def should_evaluate(
        self, *, now: datetime, snapshot: MarketSnapshot,
    ) -> bool: ...

    async def evaluate(
        self, snapshot: MarketSnapshot,
    ) -> ModelOpinion | None: ...
```

`should_evaluate` lets each brain honor its own cadence: MC pulses at the fastest useful interval (~15s), brains no-op the ticks that aren't theirs.

Brains never see: `seat_key`, `pulse_id`, `snapshot_id`, the trader, Mongo, or the arbiter. Those live above them.

---

## 4. Pulse loop

Critical path — must not block on maintenance work.

```python
async def pulse_tick() -> PulseReceipt:
    pulse = await begin_pulse()                     # allocates pulse_id
    snapshots = await snapshot_service.build_all()  # immutable, one per (lane, symbol)

    jobs = [
        evaluate_brain(brain, snapshot, pulse)
        for snapshot in snapshots
        for brain in registry.for_lane(snapshot.lane)
        if brain.should_evaluate(now=pulse.started_at, snapshot=snapshot)
    ]
    results = await asyncio.gather(*jobs, return_exceptions=False)
    # `evaluate_brain` catches its OWN exceptions — return_exceptions=False
    # here means "gather raises only if evaluate_brain itself is broken",
    # which is orchestration failure, not brain failure.

    envelopes = [r for r in results if r is not None]
    await opinion_store.upsert_many(envelopes)      # idempotent by (pulse_id, brain_id, symbol, lane)

    seat_batches = group_for_arbitration(envelopes)
    arbitrations = await arbitrate_batches(
        seat_batches, pulse_id=pulse.id, runtime_mode=runtime_mode,
    )

    # Non-critical maintenance — enqueue only, never block the pulse:
    await grader.enqueue_due_grades(as_of=pulse.started_at, pulse_id=pulse.id)

    return await complete_pulse(
        pulse, evaluations=results, arbitrations=arbitrations,
    )
```

Grader work happens on a **separate worker loop**. If Atlas is slow or a 15m price lookup stalls, the next pulse still fires on time.

---

## 5. Containment

One broken brain never silences the others.

```python
async def evaluate_brain(brain, snapshot, pulse):
    try:
        opinion = await asyncio.wait_for(
            brain.evaluate(snapshot),
            timeout=brain.evaluation_timeout_seconds,
        )
    except asyncio.TimeoutError:
        await record_brain_failure(brain.id, snapshot, pulse, "evaluation_timeout")
        return None
    except Exception as exc:
        await record_brain_failure(brain.id, snapshot, pulse, "evaluation_error", exc=exc)
        return None

    if opinion is None:
        return None

    return OpinionEnvelope(
        pulse_id=pulse.id,
        brain_id=brain.id,
        seat_key=roster.seat_for(brain.id, lane=snapshot.lane, symbol=snapshot.symbol),
        snapshot_id=snapshot.snapshot_id,
        opinion=opinion,
        evaluated_at=clock.now(),
    )
```

---

## 6. Idempotency

Every pulse has a unique `pulse_id`. Two Mongo uniqueness constraints prevent duplicate executable decisions across pulse retries:

- `mc_seats`: unique `(pulse_id, brain_id, symbol, lane)` — a brain's opinion is upserted per pulse, never duplicated.
- `mc_arbitrations`: unique `(pulse_id, seat_key)` — one arbitration per seat per pulse.

Restarting a partially-completed pulse **completes missing work**; it cannot create a second executable decision.

The existing `seat_key = "{lane}:{symbol}:{5min_bucket}"` becomes the *slot* identifier; `pulse_id` becomes the *execution* identifier. A single seat can see multiple pulse arbitrations within its 5-min bucket — only the last-arbitrated one carries `intent_id` if LIVE.

---

## 7. Pulse receipt

Reports orchestration health AND per-brain health. Never conflate them.

```python
@dataclass
class PulseReceipt:
    pulse_id: str
    started_at: datetime
    completed_at: datetime | None
    snapshot_count: int
    brains_expected: int
    brains_completed: list[str]
    brains_failed: list[dict]           # [{brain, reason, exc_type}, ...]
    arbitrations_completed: int
    intents_emitted: int                # 0 while DISARMED
    overrun: bool                       # completed_at - started_at > cadence
    grader_enqueued: int
```

Written to `mc_pulses` (TTL 7 days) and mirrored onto `brain_runtime_metrics.risedual_stack.pulse.*` for the dashboard.

"Pulse healthy" MUST require `brains_failed == []`. A green pulse with a dead brain is exactly the dishonesty the 3-clock work exists to prevent.

---

## 8. What runners hide today — MUST be captured explicitly before deletion

Runner code contains implicit contracts. Before deleting any runner, MC must own each of these explicitly:

- **Normalization** — symbol casing, tf naming, quote timestamp rounding
- **Freshness rejection** — quotes / bars older than N seconds get skipped
- **Cadence rules** — some brains tick every 30s, some 60s, some only on session boundaries
- **Roster attribution** — which brain is assigned which seat/symbol
- **Runtime modes** — DISARMED vs LIVE (now owned by arbiter)
- **Evidence stamping** — what goes into the intent's `evidence` field
- **Exception behavior** — which errors halt the tick, which are logged and continued
- **Deduplication** — same-tick same-symbol re-emission guards

An audit checklist for each runner MUST be produced before that runner is deleted.

---

## 9. Personality preservation

Real disagreement comes from doctrine, not timing accidents. Synchronizing snapshots will remove some *fake* disagreement (drift between runners fetching data at different microseconds). That's a GOOD loss.

Each brain retains its own:

```python
class CaminoBrain:
    doctrine = CaminoDoctrine()
    memory = CaminoState()
    thresholds = CaminoThresholds()
    async def evaluate(self, snapshot): ...

class BarracudaBrain:
    doctrine = BarracudaDoctrine()
    memory = BarracudaState()
    thresholds = BarracudaThresholds()
    async def evaluate(self, snapshot): ...
```

State stored under distinct namespaces: `mc_brain_state.{brain_id}.{strategy_version}`. Never a shared "rolling state" object across brains.

---

## 10. Migration order (8 steps, one brain at a time)

1. **Pulse infra** — registry, immutable `MarketSnapshot`, `OpinionEnvelope`, `PulseReceipt`, idempotency contracts (unique indexes), begin_pulse / complete_pulse
2. **Adapt ONE brain** (simplest first — TBD) to `.evaluate(snapshot)`. Keep its runner running.
3. **Comparison-only mode** — pulse calls the adapted brain in parallel with runner; opinions written to `mc_opinions_compare` (NOT `mc_seats`, NOT arbitrated). No duplicate submission.
4. **Confirm parity** — action rate, confidence distribution, reason_codes overlap, timestamp behavior all within acceptable drift.
5. **Move remaining brains one at a time**, repeating steps 2–4.
6. **Switch arbitration input** to pulse-owned `OpinionEnvelope`s (arbiter reads from `mc_seats` populated by pulse, not by runner direct-write).
7. **Delete runner scheduling and direct writes** — only after every brain is on pulse AND arbitration reads pulse envelopes.
8. **Remove sidecar identity + heartbeat plumbing** ONLY after grep confirms no reader depends on `sidecar_checkins` collection, `shared_heartbeats` fields, or `bump_stack_heartbeat` callers.

Do NOT attempt a big-bang rewrite. Every step must be individually revertable.

---

## 11. Migration acceptance tests — personality separation

Parity of outputs is NECESSARY but INSUFFICIENT. Tests must also prove the brains are still four distinct minds:

```python
def test_camino_and_barracuda_have_distinct_reason_codes(pulse_replay):
    assert camino_opinion.reason_codes != barracuda_opinion.reason_codes

def test_action_distribution_stays_diverse(session_replay):
    # Over one session, fraction of ticks where all 4 brains chose
    # the same action must stay below a threshold.
    assert distinct_action_rate >= 0.35

def test_pairwise_confidence_correlation_bounded(session_replay):
    # Two brains that always agree are one brain in two skins.
    for a, b in itertools.combinations(brains, 2):
        assert pairwise_confidence_correlation(a, b) < 0.85

def test_brain_specific_features_are_used(session_replay):
    # Camino must actually read its momentum features; Barracuda
    # must actually read its mean-reversion features. Assert via
    # the snapshot-access trace each brain writes on evaluate().
    assert camino_uses("momentum_20d")
    assert barracuda_uses("bollinger_pct_b")
```

If these tests fail during migration, the doctrine-collapse risk is real and the migration must halt.

---

## 12. Data locations

- **`mc_pulses`** (new): `_id = pulse_id`, holds `PulseReceipt`. TTL 7 days.
- **`mc_seats`** (existing from arbiter Phase 1): now ALSO carries `pulse_id` on the winning row.
  - New unique index: `(pulse_id, brain_id, symbol, lane)`
- **`mc_arbitrations`** (new): one row per completed arbitration, unique on `(pulse_id, seat_key)`. Stores the decision snapshot + intent_id.
- **`mc_brain_state.{brain_id}.{strategy_version}`** (new pattern): each brain's namespaced rolling state. NEVER a shared rolling-state object.
- **`brain_runtime_metrics.risedual_stack.pulse.*`**: mirror of the latest `PulseReceipt` for the dashboard.
- **`mc_opinions_compare`** (temporary, phase 3 of migration): parity comparison rows. Deleted at step 7.

---

## 13. Directory layout

```
/app/backend/mc_pulse/
    __init__.py
    pulse.py                # begin_pulse, pulse_tick, complete_pulse
    snapshot.py             # MarketSnapshot (frozen), SnapshotService
    registry.py             # brain registry, roster.seat_for()
    envelope.py             # OpinionEnvelope, upsert_many
    receipt.py              # PulseReceipt dataclass + persistence
    containment.py          # evaluate_brain, record_brain_failure
    protocols.py            # Brain protocol (typing only)
    tests/
        test_snapshot_immutability.py
        test_containment.py     # broken brain doesn't silence others
        test_idempotency.py     # retry pulse → no double-write
        test_receipt_shape.py

/app/backend/mc_brains/     # one file per brain, all implementing Brain protocol
    camino.py               # CaminoBrain (adapted from external/brains — Phase 2)
    barracuda.py            # ...
    hellcat.py              # ...
    gto.py                  # ...
```

---

## 14. What survives from mc_arbiter/

Everything I built this session stays — pulse is an additional layer, not a replacement.

- `models.py` (ModelOpinion, DaweState, Direction, RuntimeMode) — reused as-is
- `seat_key.py` — reused as-is
- `dawe.py` — reused as-is
- `arbiter.py` — reused; `submit_opinion` becomes an internal call from `pulse.upsert_many`
- `grader.py` — moves to a separate background worker (not on critical pulse path)
- `routes.py` — kept for admin/debug injection + runtime-mode flip; brains no longer POST here

---

## 15. Sign-off checkboxes

- [ ] Doctrine "MC owns orchestration; brains own interpretation" approved
- [ ] Immutable snapshot with per-brain isolation approved
- [ ] Idempotency via `pulse_id` + unique `(pulse_id, brain_id, symbol, lane)` approved
- [ ] Grader moved off the critical pulse path approved
- [ ] Pulse receipt exposes per-brain health approved
- [ ] Migration order (8 steps, one brain at a time, comparison-only before switch) approved
- [ ] Personality separation acceptance tests approved
- [ ] Directory layout (`mc_pulse/` + `mc_brains/`) approved
