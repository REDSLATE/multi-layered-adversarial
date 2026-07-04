# Sign-off: observation-receipts marooned under legacy brain names

**Status:** DRAFT — awaiting operator sign-off. Not applied.  
**Scope:** `backend/shared/observation_receipts.py` (endpoint alias resolution) + one-shot migration script.  
**Blast radius:** observation_receipts collection only. No market-data exposure. No gate logic. No execution path.  
**Doctrine coverage:** boundary-canonicalization + data migration. Same pattern as the existing `canonicalize_stack()` used by `intents.py`.

## 1. Problem

`observation_receipts` collection has **8,553 rows filed under legacy brain names** that the list endpoint cannot serve:

```
alpha/equity:    42 rows
camaro/crypto:    1 row
camaro/equity: 8510 rows
```

The endpoint at `backend/shared/observation_receipts.py:210–212` enforces `brain in RUNTIMES` where `RUNTIMES = ("camino", "barracuda", "hellcat", "gto")`. Querying with a legacy name returns `400 unknown brain 'camaro'`. Querying with the canonical name returns 0 rows because the DB has none.

Result: **the 8,553-row observation-receipt corpus is unreachable via the list endpoint**, though it's still visible via the aggregate `/counts` endpoint (which doesn't validate `brain`).

## 2. Verified upstream context (from this session's earlier investigation)

- The writer at `observation_receipts.py:134` sets `brain = intent.get("stack")` on write. If intents at write time carried legacy `stack: "camaro"`, the receipts got filed under `camaro`.
- Current intents (verified against fresh 07-04 06:12 UTC intents) now carry canonical `stack: "barracuda"` at the top level. Writer would file new receipts canonically — no new legacy-named rows should accumulate.
- However, `is_observation_candidate()` requires `size_multiplier == 0 OR would_trade_without_gates == False`. All 288 post-07-02-06:00 UTC equity HOLDs I sampled had `size_multiplier > 0 AND would_trade_without_gates == True`. **None qualify for receipt writing right now.** That's a separate observation (see §7) — the receipt writer being silent on current traffic isn't the bug this sign-off fixes.

## 3. Fix #1 — canonicalize `brain` query parameter at endpoint boundary

**File:** `backend/shared/observation_receipts.py`  
**Function:** `list_observation_receipts` at line 199

Proposed diff:

```diff
+ from shared.brain_legend import canonicalize_stack, CANONICAL_BRAINS
+
  @router.get("")
  async def list_observation_receipts(
      brain: Optional[str] = Query(default=None),
      lane: Optional[str] = Query(default=None),
      resolved: Optional[bool] = Query(default=None),
      limit: int = Query(default=50, ge=1, le=500),
      _user: dict = Depends(get_current_user),
  ):
      q: dict = {}
      if brain:
-         if brain not in RUNTIMES:
-             raise HTTPException(status_code=400, detail=f"unknown brain {brain!r}")
-         q["brain"] = brain
+         # Accept both canonical AND legacy names; canonicalize at boundary
+         # so any future orphaned data doesn't recur. Same pattern as
+         # intents.py's stack normalization.
+         canonical = canonicalize_stack(brain)
+         if canonical not in CANONICAL_BRAINS:
+             raise HTTPException(status_code=400, detail=f"unknown brain {brain!r}")
+         q["brain"] = canonical
```

**Behavior change:** query `?brain=camaro` now resolves to `camino` (via `LEGACY_TO_CANONICAL` mapping) and filters against `brain: "camino"` in the DB. Assumes the migration in §4 has run — otherwise this returns 0 rows for legacy-named data (which is no worse than current state).

## 4. Fix #2 — one-shot migration to rewrite legacy brain names to canonical

**New file:** `backend/scripts/migrate_observation_receipts_legacy_brain_names.py`

Purpose: rewrite the `brain` field on existing observation_receipts rows from legacy names (`alpha`, `camaro`, `chevelle`, `redeye`) to canonical (`camino`, `barracuda`, `hellcat`, `gto`).

Proposed script structure (idempotent, dry-run by default):

```python
"""One-shot: canonicalize `brain` field on legacy observation_receipts rows.

Usage:
    # Dry-run (default): counts how many rows would change, no writes
    python backend/scripts/migrate_observation_receipts_legacy_brain_names.py
    
    # Apply the migration
    python backend/scripts/migrate_observation_receipts_legacy_brain_names.py --apply

Idempotent: rerunning after successful apply is a no-op (no rows match
the legacy-name filter).

Non-destructive: original brain name is preserved on the doc as
`brain_original_legacy` so the migration is traceable in audit and
manually reversible if needed.
"""
import argparse, asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient

LEGACY_TO_CANONICAL = {
    "alpha":    "camino",
    "camaro":   "barracuda",
    "chevelle": "hellcat",
    "redeye":   "gto",
}

async def main(apply: bool):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    coll = db["observation_receipts"]
    
    total_touched = 0
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        count = await coll.count_documents({"brain": legacy})
        print(f"  {legacy:>8} → {canonical:<10}  {count} rows", end="")
        if count == 0:
            print(); continue
        if apply:
            result = await coll.update_many(
                {"brain": legacy},
                {"$set": {"brain": canonical, "brain_original_legacy": legacy}},
            )
            print(f"  ← updated {result.modified_count}")
            total_touched += result.modified_count
        else:
            print(f"  (dry-run — pass --apply to write)")
            total_touched += count
    print(f"\nTotal rows {'updated' if apply else 'would be updated'}: {total_touched}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually perform writes")
    args = ap.parse_args()
    asyncio.run(main(args.apply))
```

**Expected apply-run output based on current DB state:**

```
   alpha → camino      42 rows  ← updated 42
  camaro → barracuda   8511 rows  ← updated 8511
chevelle → hellcat        0 rows
  redeye → gto            0 rows

Total rows updated: 8553
```

## 5. Test package

**New file:** `backend/tests/test_observation_receipts_legacy_alias.py`

Test cases:

1. **`test_list_endpoint_rejects_unknown_brain`** — query with a genuinely-unknown brain (e.g. `?brain=nonexistent`) still returns 400. Pins the guard.
2. **`test_list_endpoint_accepts_canonical_name`** — `?brain=barracuda` returns 200. Baseline behavior.
3. **`test_list_endpoint_accepts_legacy_camaro_and_maps_to_barracuda`** — `?brain=camaro` returns 200, filters against `brain: "barracuda"` in DB. Pins the alias resolution.
4. **`test_list_endpoint_accepts_all_four_legacy_names`** — parametrized over `alpha/camaro/chevelle/redeye`. Each resolves to its canonical counterpart.
5. **`test_migration_dry_run_no_writes`** — seed a few legacy-named rows, run migration in dry-run, assert row count unchanged.
6. **`test_migration_apply_rewrites_brain_field`** — seed rows, run with `--apply`, assert `brain` field is canonical AND `brain_original_legacy` preserves the original.
7. **`test_migration_is_idempotent`** — run apply twice, second run touches 0 rows.

## 6. Deploy sequence

1. Merge Fix #1 (endpoint alias). Effect: legacy-name queries now return 200 with 0 rows, canonical-name queries still return 0 rows (DB has none under canonical yet).
2. Run migration script in dry-run first, confirm expected row counts against `/api/admin/observation-receipts/counts` output.
3. Run migration `--apply`. 
4. Verify: `/api/admin/observation-receipts?brain=barracuda&lane=equity&limit=5` now returns actual rows (was 0 before).
5. Verify: `/api/admin/observation-receipts?brain=camaro&lane=equity&limit=5` also returns rows (via alias resolution). Same data as step 4.

## 7. Sub-observation flagged, not in this fix's scope

The receipt-writer eligibility gate (`is_observation_candidate()` at `observation_receipts.py:66`) requires `size_multiplier == 0 OR would_trade_without_gates == False`. Verified in this session: 288/288 post-07-02-06:00 UTC equity HOLDs have `size_multiplier > 0 AND would_trade_without_gates == True`, so **none qualify for receipt writing.** No new receipts have accumulated on the current wave of HOLDs.

That's a separate question: is the current HOLD path supposed to produce observation-grading candidates, or is it correctly filtered out because these HOLDs aren't "honest holds" (brain-self-zeroed) but rather "argmax HOLDs" (hypothesis-hold won)? Deferred — this sign-off is scoped to unmarooning existing data, not fixing writer coverage.

## 8. Rollback plan

Fix #1 rollback: revert the four-line change to the original two-line `if brain not in RUNTIMES:` guard. Effect: legacy-name queries go back to returning 400. Migrated data still queryable via canonical names.

Fix #2 rollback: run inverse migration using the preserved `brain_original_legacy` field:

```python
# Restore legacy names — for the 8553 rows that have brain_original_legacy set
coll.update_many(
    {"brain_original_legacy": {"$exists": True}},
    [{"$set": {"brain": "$brain_original_legacy"}},
     {"$unset": "brain_original_legacy"}],
)
```

Fixes are independent — either can be rolled back without affecting the other.
