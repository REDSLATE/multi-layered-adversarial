# RISEDUAL Roadmap — refactor & cleanup backlog

This file tracks structural work that's scoped and queued but NOT
yet authorized. Operator triggers each item explicitly. Items are
ordered by ROI / risk profile, not by urgency.

---

## 2026-02-25 — Code-redundancy audit (operator-authored, deferred)

The operator commissioned an external structural audit of the
codebase (1,256 files / 818 Python). Below is the consolidated
backlog from that audit, normalized into actionable units with
my own risk/scope annotations. **NOT to be worked today —
operator explicitly deferred until after the immediate trading
priorities ship.**

### P0 (verify first — not a refactor, a possible trading blocker)

**0a. `brain_tuning_cache.get_override()` — is it actually wired in?**
- The audit claims `get_override("equity", "min_confidence")` and
  `get_override("equity", "min_gap")` exist as operator-tunable
  knobs in `shared/brain_tuning_cache.py` but the native brain
  strategies (`shared/brains/{barracuda,camino,gto,hellcat}/strategy.py`)
  still read `doctrine.min_confidence` / `doctrine.min_gap` directly
  from `shared/brain_doctrine.py`.
- **If true**: the operator UI tuning knobs are PLACEBO — adjusting
  them in the UI never reaches the brain decision math. The
  conservative-execution problem the operator has been chasing
  could be partly explained by tuning overrides simply not applying.
- **Verification step (first thing to do when work resumes)**:
  ```bash
  grep -rn "get_override" /app/backend/shared/brains/ /app/backend/shared/strategies/
  grep -rn "doctrine.min_confidence\|doctrine.min_gap" /app/backend/shared/brains/
  ```
  If the first grep returns nothing in `brains/` and the second
  returns hits in every strategy → claim confirmed → P0 fix.
- **Fix shape (if confirmed)**: each strategy reads its config
  through a thin resolver that checks the override cache first,
  falls back to doctrine:
  ```python
  min_confidence = (
      get_override(lane, "min_confidence")
      or doctrine.min_confidence
  )
  ```
- **Status**: NOT STARTED. Verification is a 30-second grep —
  do it before refactor week.

**0b. `_runner_core.py` HOLD-skip behavior**
- Audit claims:
  ```python
  if decision.action == "HOLD":
      skipped.append(...)
      continue
  ```
  …means HOLDs never get persisted, breaking the honesty-audit
  surface (which counts `would_have_traded_without_gates=True`
  intents — but those are conditional on the intent existing).
- **Likely partial truth**: HOLD intents are skipped from
  EMISSION but the honesty-audit may capture them via a
  different code path. Need to confirm.
- **Verification step**:
  ```bash
  grep -rn "would_have_traded_without_gates" /app/backend/shared/
  ```
  Confirm whether HOLD decisions ever set this field, and where.
- **Status**: NOT STARTED. Verify before deciding if any change
  is needed — could be working as designed.

### P1 — High-ROI, low-risk refactors

**1. Archive `.revert_snapshots/` out of the active tree**
- Audit estimate: ~3,900 lines of old execution/server copies.
- Move to a sibling directory (or external archive) and add a
  `.gitignore` entry so it doesn't drift back in.
- **Risk**: Zero functional impact. Rollback copies remain
  accessible if needed.
- **Acceptance**: `du -sh .revert_snapshots/` before vs. after;
  `git ls-files` no longer includes the directory.

**2. Collapse 4 `doctrine_interpreter.py` files into one**
- Locations:
  - `backend/runtimes/alpha/doctrine_interpreter.py`
  - `backend/runtimes/camaro/doctrine_interpreter.py`
  - `backend/runtimes/chevelle/doctrine_interpreter.py`
  - `backend/runtimes/redeye/doctrine_interpreter.py`
- **Important pre-step**: confirm whether the `runtimes/*/` dirs
  are still imported anywhere. The MC migration moved brains
  natively into `shared/brains/`; these runtime dirs may be
  pure dead code (largest possible win — full deletion).
  ```bash
  grep -rn "from runtimes\." /app/backend/ --include="*.py"
  grep -rn "import runtimes" /app/backend/ --include="*.py"
  ```
- **If dead**: delete the entire `runtimes/` tree (likely
  multi-thousand-line win).
- **If live**: extract one shared `interpret_doctrine(brain_id, snapshot)`
  in `shared/` and have each runtime delegate to it.
- **Status**: NOT STARTED. Dead-code check should run first.

### P2 — Medium-effort, structural

**3. Merge equity/crypto risk modules**
- Audit claims 90-95% similarity between equity & crypto copies of:
  - `stop_loss.py`
  - `take_profit.py`
  - `trailing_stop.py`
  - `max_hold_time.py`
- Verify the similarity number first — `diff` the files; if they're
  really ≥90% identical, collapse into `shared/risk_rules/*.py` with
  `lane="equity" | "crypto"` as config.
- **Risk**: Medium. Per-lane edge cases (e.g., crypto 24/7 vs.
  equity market hours) must remain expressible via config, not
  flattened away. A diff-first audit before any merge.

**4. Extract shared helpers from 4 brain strategies**
- Helpers identified by audit:
  - `_safe_float`
  - `_hold`
  - RR validation
  - evidence construction
  - confidence floor check
- **Explicit constraint from audit**: DO NOT fully merge the four
  strategies — they share structure but personalities (thresholds,
  weights, objections, doctrine) are intentionally distinct.
  Extract helpers into `shared/brains/_common/` and import. Keep
  per-brain strategy files for the personality layer.
- **Risk**: Low if helpers are pure functions. Each extraction
  needs an existing test that locks behavior before move.

### P3 — Speculative / requires deeper audit before scoping

**5. Replace `if brain == X / elif brain == Y` branching with `BrainProfile` config**
- The audit suggests a `BrainProfile(thresholds=..., evidence_weights=..., objections=...)`
  passed into a single engine.
- **My read**: This is a real direction but **already partially
  implemented** via `brain_doctrine.py` / `camaro_weights.py` /
  consensus layer. Need to audit whether the remaining
  `if brain == ...` branches are *behavioral* (warrant elimination)
  or *boundary* (broker / seat policy / audit — intentional
  separation-of-concerns that the audit flagged "do not reduce").
- **Status**: Deferred until items 1-4 ship and reveal what's
  left to consolidate.

**6. Single shared execution pipeline**
- Audit asserts `Brain → Security → Seat → Governor → RoadGuard → Broker`
  should exist once and brains should emit only `BrainVote`.
- **My read**: This IS the architecture today via
  `shared/pipeline/consensus_pool.py` + `shared/pipeline/execution.py`.
  The audit may be looking at older surface area. Confirm by
  walking the actual import graph before declaring work needed.

### Operator triggers for resuming

This roadmap unlocks when:
1. P0 trading priorities ship (Witness Verifier promotion, Public.com
   preflight, Camino-cites-evidence) AND
2. Operator confirms equity is trading reliably AND
3. Operator gives explicit go-ahead for refactor week.

Until then: do not touch any of the items above.

---
