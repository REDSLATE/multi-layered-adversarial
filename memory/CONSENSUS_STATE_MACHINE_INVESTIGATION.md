# Consensus → Broker State Machine Investigation
_Handoff doc for iter-27 doctrine steps 5.b, 7, 8_
_Written: 2026-07-11_

## Why this doc exists

Steps 5.a (consensus_fingerprint) and 6 (invalidate 683 stuck rows) both landed. But the ROOT CAUSE of "why did the 683 sit in consensus_long/short for 3 weeks without advancing" is unresolved. Also unresolved: 1,221 `proposed` + 1,146 `discussing` positions are STILL accumulating right now — a separate pre-consensus stall.

This is not a "read some code" problem — it's an investigation problem. The next session should start here.

## Concrete queries to run first (5 min of Mongo)

```python
# 1) Are consensus positions accumulating right now?
async for d in db.shared_positions.aggregate([
    {"$match": {"state": {"$in": ["consensus_long","consensus_short"]}}},
    {"$group": {"_id": "$state", "n": {"$sum": 1},
                "oldest": {"$min": "$updated_at"},
                "newest": {"$max": "$updated_at"}}},
]):
    print(d)

# 2) What states exist per lane?
async for d in db.shared_positions.aggregate([
    {"$group": {"_id": {"lane": "$lane", "state": "$state"}, "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
]):
    print(d)

# 3) Was there ever a successful advance PAST consensus?
async for d in db.shared_positions.find(
    {"state": {"$in": ["pending_open", "submitted", "held", "open"]}},
    {"_id": 0, "state": 1, "updated_at": 1, "symbol": 1, "direction": 1}
).sort("updated_at", -1).limit(5):
    print(d)
# If this returns 0 rows — the pipeline was NEVER completed for anyone

# 4) Search audit log for the transition that SHOULD happen
async for d in db.shared_audit.find(
    {"kind": {"$in": ["broker_submit", "position_open", "pending_open"]}}
).sort("at", -1).limit(5):
    print(d)
# If empty — no broker submission has ever been attempted from consensus_*
```

## Files to inspect (specific line numbers)

**`/app/backend/shared/positions.py`:**
- Line 631-643 (now instrumented at line 631+): the `consensus_long/short` transition — this is WORKING (683 positions got here)
- Line 677: `SHARED_POSITIONS.update_one` in `_persist_stance` — stance-related updates
- Line 736: another update site — inspect what state it's transitioning FROM/TO
- Line 779: another — same question
- Line 810: TTL/aging query on `OPEN_STATES` — is `consensus_long/short` in `OPEN_STATES`?

**Search for who transitions PAST consensus:**
```bash
grep -rn "pending_open\|STATE_PENDING\|from.*consensus" /app/backend --include="*.py" | grep -v __pycache__
```

Likely candidates for the transition worker:
- `/app/backend/shared/positions.py` — check if there's an `_advance_consensus_to_pending_open()`
- `/app/backend/routes/positions.py` or `/app/backend/routes/consensus.py` — API-triggered advance
- `/app/backend/shared/executor*.py` — the executor subsystem
- Any file with `pending_open` write logic

**Search for the missing worker:**
```bash
grep -rn "consensus_long.*pending_open\|consensus_long.*broker\|consensus_.*advance" /app/backend --include="*.py" | grep -v __pycache__
```

## Hypotheses (ranked by prior probability)

1. **No worker was ever built for consensus → pending_open.** The transition is manually triggered via an API endpoint that isn't being called. 683 positions sit forever because no automation exists. Look for `/api/positions/{id}/advance` or similar in the routes.

2. **Worker exists but filters on wrong state.** E.g. queries `status="consensus"` while records store `state="consensus_long"`. Field name mismatch is a very common cause of "workers that don't do anything."

3. **Seat authorization mismatch.** The transition requires the executor seat, but the seat is vacant / held by a brain that doesn't match some check. Check `_executor_seat()` and `seat_may_execute_lane`.

4. **Broker circuit-breaker open.** The Webull broker submission fails and marks itself unavailable, but the failure logs are silent. Check `shared/broker/*.py` for circuit-breaker state.

5. **Missing `next_attempt_at` field.** A retry worker only picks up rows with `next_attempt_at <= now()`; if consensus rows never get this field, they're invisible to the worker.

## Doctrine reminder (from the operator, 2026-07-11)

> Every terminal or blocked branch must write a reason:
>
> ```python
> transition_attempt = {
>     "from": "consensus_long",
>     "to": None,
>     "status": "blocked",
>     "stage": "lane_gate",
>     "reason_code": "EQUITY_LANE_CLOSED",
> }
> ```
>
> There should be no silent return such as:
>
> ```python
> if not eligible:
>     return
> ```
>
> Replace it with a persisted disposition:
>
> ```python
> if not eligible:
>     await reject_transition(
>         position_id,
>         reason_code="STALE_CONSENSUS_INPUT",
>     )
>     return
> ```

That's the shape of Step 7. Every `return` in the transition workflow that isn't a happy-path advance MUST persist a reason to a new `shared_position_transitions` collection (or as a nested audit doc).

## Deferred from Step 5 (5.b — fresh-input gate)

Landed in Step 5.a: `consensus_fingerprint = sha256(v1|symbol|direction|engaged_brains)` + sparse unique index.

**NOT LANDED (Step 5.b):**
- Stances don't yet carry `source_bar_close_at` — data plumbing from the intent evidence into the stance write in `_persist_stance` at line 654-680 of `positions.py`. Once landed, bump `consensus_fingerprint_version` from `v1` to `v2` and include `min(stance_bar_closes)` in the hash input
- Fresh-input gate: BEFORE writing consensus_long/short, verify all engaged brains' most-recent stances derived from bars within a freshness tolerance of each other (e.g. all within one tf-bucket). Reject with `STALE_CONSENSUS_INPUT` if not

## Working code paths to preserve

DO NOT touch these during Steps 7/8:

- `_auto_advance_from_executor_stance()` — this already advances proposed → consensus_long/short correctly, and now has consensus_fingerprint dedup
- `_persist_stance()` — stance write, works fine
- The `mc_pulse` subsystem — orthogonal to this work
- All feeder work — orthogonal

## Success criteria for the next session

1. **New consensus positions advance:** watch a fresh consensus_long row transition to pending_open within 60s of the state change
2. **Blocked transitions surface visibly:** any transition that CAN'T advance writes a reason code to `shared_position_transitions` (or equivalent)
3. **Pre-consensus stall investigated:** understand why 1,221 `proposed` + 1,146 `discussing` positions are accumulating. May be same worker, may be different
4. **Regression suite still 205/205**
