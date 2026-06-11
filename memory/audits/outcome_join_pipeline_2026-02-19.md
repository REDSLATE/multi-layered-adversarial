# Outcome-Join Pipeline — Audit Report

**Date:** 2026-02-19
**Author:** E1 (forked session)
**Status:** Code complete, end-to-end wiring verified, **0/100 LEARNING counters waiting on live closed trades** to accrue.
**Scope:** Read-only audit. No code changes proposed here — this report is the deliverable.

---

## 1. The five-link chain (end-to-end)

```
┌─────────────────────┐    intent_id     ┌─────────────────────┐
│  POST /api/intents  │ ────────────────▶│  doctrine_sidecars  │ ← audit row written here
│  (brain ingest)     │                  │   (intent-keyed)    │   with quality/seat/scores
└──────────┬──────────┘                  └──────────▲──────────┘
           │                                        │
           │ intent_id                              │ outcome_join envelope
           ▼                                        │ written here on close
┌─────────────────────┐  receipt_id      ┌──────────┴──────────┐
│ /execution/submit   │ ────────────────▶│ shared_live_positions│
│ (gates + broker)    │  + intent_id      │  (position-tracking)│
└─────────────────────┘                  └──────────┬──────────┘
                                                    │
                                                    │ live_positions.close()
                                                    ▼
                                         ┌─────────────────────┐
                                         │  shared_outcomes    │ ← also written so the
                                         │   (legacy scorecard)│   pre-doctrine scorecard
                                         └─────────────────────┘   pipeline keeps working
```

### Link 1 — Intent ingest writes the doctrine sidecar
**File:** `backend/shared/intents.py:589`
**Code:** `await db[DOCTRINE_SIDECARS].insert_one(audit_row.copy())`
**Keys persisted:** `intent_id` (PK), `stack`, `lane`, `symbol`, `quality`, `score`, `doctrine_version`, full seat-doctrinal hoist (strategist/adversary/governor/execution_judge actions + holders), `snapshot`, `packet`, `ts`.
**Verdict:** ✅ Correct. Audit row exists for every brain-emitted intent BEFORE the gate chain runs. If the gate chain blocks the intent, the doctrine row stays orphaned (which is fine — gate blocks have no outcome).

### Link 2 — Receipt carries `intent_id` through `/execution/submit`
**File:** `backend/shared/execution.py:execution_submit` (reads `intent_id` from the intent doc and stamps it onto the broker receipt + downstream calls).
**Verdict:** ✅ Correct. The receipt schema includes `intent_id`; the broker fill response is wrapped in a receipt with the same key.

### Link 3 — Live position open preserves `intent_id`
**File:** `backend/shared/live_positions.py:open_from_receipt` (lines 87–177)
**Key line:** `doc["intent_id"] = intent_id` (line 128)
**Idempotency:** by `receipt_id` (line 101) — re-running `open_from_receipt` on the same receipt returns the existing position; no duplicate row, no duplicate Shelly write.
**Verdict:** ✅ Correct.

### Link 4 — Close fires `join_outcome_to_doctrine`
**File:** `backend/shared/live_positions.py:close` (lines 249–404)
**Key block (lines 358–380):**
```python
try:
    from shared.doctrine.outcome_join import join_outcome_to_doctrine
    await join_outcome_to_doctrine(
        intent_id=pos.get("intent_id"),
        position_id=position_id,
        lane=pos.get("lane"),
        symbol=pos.get("symbol"),
        outcome_label=label,
        pnl_usd=pnl_usd, pnl_pct=pnl_pct,
        opened_at=pos.get("opened_at"),
        closed_at=now,
        closing_actor=actor,
        extra={"stack": ..., "direction": ..., "outcome_broadcast_id": ...},
    )
except Exception:
    pass  # advisory — never block the close
```
**Verdict:** ✅ Correct. Fail-soft is correct doctrine here: a join failure must never block the position from going to STATE_CLOSED. The backfill path (`/api/admin/outcome-join/backfill`) is the recovery mechanism for any miss.

### Link 5 — `join_outcome_to_doctrine` writes the envelope
**File:** `backend/shared/doctrine/outcome_join.py` (113 lines)
**Match key:** `intent_id`. Idempotent — uses `{"intent_id": ..., "outcome_join": {"$exists": False}}` filter so a double-fire (take-profit AND manual close) cannot double-attach.
**Envelope shape:**
```jsonc
{
  "joined_at": "<ISO>",
  "position_id": "...",
  "lane": "equity|crypto",
  "symbol": "AAPL",
  "outcome_label": "win|loss|scratch|stopped_out",
  "pnl_usd": 5.42, "pnl_pct": 0.018,
  "opened_at": "<ISO>", "closed_at": "<ISO>",
  "closing_actor": "take_profit_guard|operator|...",
  "max_adverse_excursion_usd": null,   // placeholder
  "max_favorable_excursion_usd": null, // placeholder
  "extra": { ... }
}
```
**Verdict:** ✅ Correct. Idempotency via the `$exists: false` guard is the right defense against race conditions.

---

## 2. What could STILL silently fail

These are the failure modes that aren't bugs per se — they're real-world conditions where the chain has nothing to teach because an upstream link broke.

| # | Failure mode | What you'll see | Detection |
|---|---|---|---|
| 1 | Intent posted WITHOUT a doctrine packet (legacy ingest path, sidecar offline) | No `doctrine_sidecars` row exists, `join_outcome_to_doctrine` returns `False` silently | `/api/admin/outcome-join/health.skipped_no_sidecar` counter |
| 2 | Intent NEVER reached the broker (blocked by a gate) | Position never opens, no close, no join — correct behavior, nothing to teach | `doctrine_sidecars.outcome_join` field stays absent (blocked intents have no outcome to attach) |
| 3 | Manual operator-opened position with no carrying `intent_id` | `open_from_receipt` returns None on line 100; no position row written | Operator opens via the `paper-open` legacy endpoint, NOT the doctrinal flow |
| 4 | Position closes BEFORE the audit row is finalized | Race: doctrine_sidecars insert is `await` but happens AFTER first-touch on the gate chain; if the position somehow closes in <1 ms (impossible in practice) the join row wouldn't exist yet | Not observed; close path is always seconds+ after open |
| 5 | `outcome_label` is `None` when `pnl_usd` is `None` | The `close` function defaults `label = None` when neither is supplied; the envelope gets `outcome_label=None`. Scorecard treats this as neither win nor loss → sample is bucketed but excluded from win-rate. | `doctrine_sidecars.outcome_join.outcome_label = null`; visible in `/api/admin/doctrine/scorecard.by_quality.{quality}.scratches` count |
| 6 | Backfill missed because the close was older than `older_than_hours` cutoff OR the row was hard-deleted | Sidecar row never gets an envelope; closed position counted as "orphan" in health check | `/api/admin/outcome-join/health.closed_position_sample.orphans_in_sample > 0` |

---

## 3. The 0/100 LEARNING counter wiring

The "100" in "0/100" comes from `shared/doctrine/scorecard.py:149`:
```python
samples_with_outcome = sum(b["samples"] for b in by_quality.values())
if samples_with_outcome < 100:
    blockers.insert(0, f"min_samples<100 (have {samples_with_outcome})")
```

The blocker disappears when `samples_with_outcome >= 100`. Each sample = one `doctrine_sidecars` row with an `outcome_join` envelope attached. So the counter literally counts joined-and-closed intents.

**Expected accrual rate at $3–$10 Webull caps:** at 5 intents/30s auto-router tick = 600 intents/hr, with realistic gate-pass rate of ~10% and same-day close rate of ~50%, the counter should hit 100 within ~3-4 hours of live trading.

**Until then, `ready_for_promotion=false`** and the `blockers` list will always include `min_samples<100`. That is by design (sample-size hygiene before any promotion).

---

## 4. What to watch on tomorrow's tape

Five operator-facing signals during the SpaceX IPO trading day:

1. **Health endpoint:** `GET /api/admin/outcome-join/health`
   - `closed_position_sample.join_rate_in_sample` should be ≥ 0.95 by EOD. Anything <0.8 means link 4 or 5 is dropping joins.
2. **Sidecar growth rate:** `totals.doctrine_sidecars` should grow at ≈ intent-ingest rate; if it lags, link 1 is failing silently.
3. **Joined growth rate:** `totals.doctrine_sidecars_joined` should grow at ≈ position-close rate; if it lags, link 4 or 5 is failing.
4. **Orphan examples:** `closed_position_sample.first_orphan_examples` — if non-empty, click through to verify each has a `doctrine_sidecars` row that just missed the join. If the row is missing entirely, link 1 dropped.
5. **Scorecard sample count:** `GET /api/admin/doctrine/scorecard` — `samples_with_outcome` should tick up monotonically. If it flatlines, something upstream is broken even though `health` looks fine.

If signals 1–5 all look clean and `samples_with_outcome` is still 0/100 by midday, the issue is upstream of the join (intents not closing) — NOT in the outcome-join layer.

---

## 5. Backfill safety net

If the live join misses a batch (network glitch, mongo flap, code deploy mid-close), the recovery is:

```bash
# Dry run — see what would be backfilled
curl -X POST $API_URL/api/admin/outcome-join/backfill \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true, "limit": 500}'

# When the dry-run output looks right, write it:
curl -X POST $API_URL/api/admin/outcome-join/backfill \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false, "limit": 500}'
```

The backfill is **idempotent** — running it twice is safe. The `$exists: false` filter in `join_outcome_to_doctrine` prevents double-attach, and the request reports `skipped_already_joined` so you can see exactly how many rows were already covered.

---

## 6. Verdict

The outcome-join pipeline is **correctly wired end-to-end**. There is no code bug holding back the 0/100 counter — it is waiting on live closed trades. The five operator-facing signals above are enough to detect any future drift. The backfill endpoint is the safety net for any miss.

**Recommendation:** No code changes. Watch signals 1–5 on the SpaceX IPO trading day; if `samples_with_outcome` is still 0/100 at midday with healthy intent ingest, the issue is upstream of the join layer.
