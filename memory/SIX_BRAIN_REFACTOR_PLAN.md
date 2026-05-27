# Mission Control — Six-Brain Expansion Refactor Plan

**Status**: PLAN ONLY (not yet implemented)
**Drafted**: 2026-05-27 (pass #12 follow-up)
**Owner**: pending — recommended **separate fork**, not main-branch development
**Estimated effort**: 8-12 hours of focused work, broken into 5 phases

---

## 0. Executive summary

Expand Mission Control from 4 brains to 6 (or arbitrary N) without breaking the 480-tripwire baseline. The current architecture has the brain identity tuple `("alpha", "camaro", "chevelle", "redeye")` hardcoded across **27+ files** — primarily as Pydantic `Literal[...]` constraints, validation loops, and test fixtures.

**Key insight from auditing the codebase**: this is NOT a simple "find-and-replace the tuple" refactor. The current design has **deliberate role-runtime coupling** anchored in `namespaces.py:ROLE_ANCHORS`:

```python
ROLE_ANCHORS = {
    "strategist": "alpha",
    "executor":   "camaro",
    "governor":   "chevelle",
    "opponent":   "redeye",
    "memory":     "shelly",
}
```

That table is **operator-locked** (per the comment block at `namespaces.py:442-445`: *"the whole point of fixing the anchors is that they DO NOT move"*). Adding brains 5 + 6 is therefore a **role-architecture decision before it's a code refactor**.

### Critical pre-decision

**Before any code is written, the operator must decide:**

1. **What ROLES do the new brains hold?** Options:
   - **Pure expansion** — add 2 new roles (e.g. `crypto_strategist`, `crypto_opponent`) anchored to 2 new brains. Doctrine stays clean: 1 brain = 1 role.
   - **Lane-specific duplication** — keep the same 4 roles but anchor crypto-lane roles to separate brains (Alpha = equity strategist, NewBrain = crypto strategist). Roles become `(lane, role)` tuples.
   - **Pool model** — make `ROLE_ANCHORS` softer: any brain CAN hold any role; seat assignment grants the runtime. (This is the biggest doctrinal shift; most invasive.)

2. **What are the new brains called?** Names need to be picked and locked before refactor starts. Suggested naming consistent with the current pattern (American muscle cars):
   - `mustang`, `corvette`, `barracuda`, `firebird`, `challenger`, `gto`, `dart`
   - Operator picks 2 (or N − 4) names. These will appear in every Pydantic `Literal[...]`, every test fixture, every doctrine doc.

3. **Do we keep the strict role-runtime coupling, or move to pool model?** This is the big architectural question. **Strict coupling is simpler to refactor** (just extends the existing constants). **Pool model is more flexible long-term** but requires rewriting the entire seat-assignment + roster logic.

**Recommended path**: **Pure expansion with strict coupling**. Pick 2 new roles + 2 new brains. Keeps the doctrine tight and lets us refactor mechanically without re-thinking authority.

---

## 1. Architecture audit — what's actually hardcoded

### 1a. Backend Python — 27 files reference the 4-brain tuple

| File | Reference type | Count |
|---|---|---|
| `namespaces.py` | `RUNTIMES`, `LIVE_RUNTIMES`, `DISCUSSION_PARTICIPANTS`, `ROLE_ANCHORS` | Source of truth |
| `shared/roster.py` | `BRAINS`, `BrainT = Literal[...]` | 2 |
| `shared/ingest.py` | 6 × `Literal[...]` field constraints | 6 |
| `shared/intents.py` | 1 × `stack: Literal[...]` | 1 |
| `shared/brain_lane_policy.py` | `KNOWN_BRAINS` tuple + 2 × `Literal[...]` | 3 |
| `shared/executor_seat.py` | 1 × `Literal[...]` | 1 |
| `shared/opinions.py` | 1 × `Literal[...]` | 1 |
| `shared/auditor_seat.py` | 1 × `Literal[...]` | 1 |
| `shared/positions.py` | `BrainT = Literal[...]` | 1 |
| `routes/learning_scoreboard.py` | hardcoded iteration loop | 1 |
| **Tests** (15 files) | Set/list assertions + iteration loops | ~20 references |

### 1b. Frontend — affected components

| File | What references brain identity |
|---|---|
| `src/lib/api.js` | `RUNTIME_META` dict — color + label per brain |
| `src/pages/Overview.jsx` | Per-runtime cards (4-up grid) |
| `src/pages/Discussion.jsx` | Per-brain opinion filters |
| `src/pages/Scorecards.jsx` | Per-brain scorecard grid |
| `src/components/LivePulse.jsx` | Per-brain badges |
| `src/pages/RuntimeDetail.jsx` | Per-brain detail pages |
| `src/components/ParadoxRosterPanel.jsx` | Role-anchor display |
| `src/components/RosterPanel.jsx` | Assignable roster grid |

### 1c. Tests — 15 files have hardcoded brain sets

These break on schema change. The strategy is to migrate them to a `LIVE_RUNTIMES` import from `namespaces` so adding a brain auto-extends the test fixtures.

### 1d. Database collections that store `brain` / `stack` / `runtime` values

These don't need migration (existing rows keep working) but the new brain names must be registered before any sidecar attempts to write:

- `shared_heartbeats.runtime`
- `sovereign_state.brain`
- `sovereign_audit_log.brain`
- `shared_intents.stack`
- `shared_brain_opinions.runtime`
- `shared_brain_memories.brain`
- `shared_brain_conflicts.participants[].runtime`
- `runtime_heartbeats.runtime`
- ~15 more

---

## 2. Recommended refactor strategy — "Open-Set Pattern"

**Core idea**: stop using Pydantic `Literal[...]` for brain identity. Replace with a runtime-validated string that's checked against `namespaces.LIVE_RUNTIMES`.

### 2a. Why move away from `Literal[...]`?

Current pattern:
```python
class IntentIn(BaseModel):
    stack: Literal["alpha", "camaro", "chevelle", "redeye"]
```

**Problem**: every time we add a brain, this constraint has to change in 12+ places. Worse, Pydantic `Literal[...]` is a compile-time constraint — you can't extend it at runtime when the operator adds a new brain.

### 2b. The open-set pattern

```python
# In namespaces.py — the ONE source of truth
LIVE_RUNTIMES: tuple[str, ...] = (
    "alpha", "camaro", "chevelle", "redeye",
    # Phase 1 expansion — add new brains here, that's it:
    "mustang", "corvette",
)

# In shared/_validators.py — new helper module
def _validate_runtime(v: str) -> str:
    """Pydantic field validator. Replaces Literal[...] constraint
    while preserving doctrine: only known brains can post."""
    v = v.lower()
    if v not in LIVE_RUNTIMES:
        raise ValueError(
            f"runtime must be one of {LIVE_RUNTIMES}, got {v!r}"
        )
    return v
```

Then everywhere previously using `Literal[...]`:

```python
# BEFORE
class IntentIn(BaseModel):
    stack: Literal["alpha", "camaro", "chevelle", "redeye"]

# AFTER
class IntentIn(BaseModel):
    stack: str  # validated below
    
    _validate_stack = field_validator("stack")(_validate_runtime)
```

**Benefits:**
- Adding a brain becomes a 1-line change in `namespaces.py`
- Tripwires can use `LIVE_RUNTIMES` import → auto-extend on expansion
- Error messages are clearer ("got 'mustng', valid: ...")
- Doctrine preserved: unknown brain identities still rejected

**Tradeoff**: lose Pydantic's IDE auto-complete on the field value. Acceptable — `LIVE_RUNTIMES` is operator-managed, not developer-managed.

---

## 3. Phased rollout — 5 phases, each independently testable

### Phase 1 — Centralize the validator (no behavior change)

**Goal**: Replace all `Literal["alpha", "camaro", "chevelle", "redeye"]` with the validator pattern. The 4-brain tuple stays unchanged. Zero functional change; just plumbing.

**Files to modify** (in this order — dependency-safe):
1. Create `shared/_validators.py` with `_validate_runtime` + `_validate_runtime_optional`
2. `shared/roster.py` — replace `BrainT` Literal with `str` + validator
3. `shared/ingest.py` — 6 fields migrated
4. `shared/intents.py` — `stack` field migrated
5. `shared/brain_lane_policy.py` — 2 fields + `KNOWN_BRAINS` deleted (import from namespaces)
6. `shared/executor_seat.py` — `Literal` → validator
7. `shared/opinions.py` — `runtime` field migrated
8. `shared/auditor_seat.py` — `Literal` → validator
9. `shared/positions.py` — `BrainT` migrated

**Tripwires to add** (before changing any code):
- `test_runtime_validator_accepts_all_live_runtimes` — every brain in `LIVE_RUNTIMES` is valid
- `test_runtime_validator_rejects_unknown` — `"foo"` raises ValidationError
- `test_runtime_validator_normalizes_case` — `"ALPHA"` → `"alpha"`
- `test_intent_in_accepts_alpha_stack` — regression guard
- `test_opinion_in_accepts_camaro_runtime` — regression guard

**Tests that will need updates**: 15 test files migrate from hardcoded set `{"alpha", "camaro", "chevelle", "redeye"}` to `set(LIVE_RUNTIMES)`. Each is a one-line change.

**Verification**:
- All 480 tripwires still pass
- End-to-end: post an Alpha intent + a REDEYE opinion via curl — both succeed
- Lint clean

**Estimated effort**: 2-3 hours.

---

### Phase 2 — Test fixture migration

**Goal**: Eliminate hardcoded brain tuples from tests. Every test that iterates brains imports from `namespaces.LIVE_RUNTIMES`.

**Files**:
- `test_roster.py`, `test_brain_emission_diagnose.py`, `test_sidecar_checkin.py`, `test_paradox_wake.py`, `test_conflict_matrix.py`, `test_heartbeat_ping.py`, `test_storage_rollup.py`, `test_governor_exclusivity_doctrine.py`, `test_role_scoring.py`, `test_paradox_namespace.py`, `test_brain_memory_ingest.py`, `test_discussion_layer.py`, `test_ladder_phase_2_and_3.py`, `test_cross_brain_memories.py`, `test_authority_collapse_and_token_audit.py`, `test_brain_memory_translator.py`, `test_contribution_health.py`

**Pattern transformation**:
```python
# BEFORE
for brain in ("alpha", "camaro", "chevelle", "redeye"):
    ...

# AFTER  
from namespaces import LIVE_RUNTIMES
for brain in LIVE_RUNTIMES:
    ...
```

**Verification**: 480 tripwires still pass.

**Estimated effort**: 1-2 hours.

---

### Phase 3 — Role anchor expansion (doctrine decision required)

**Goal**: Extend `ROLE_ANCHORS` to cover the new brains. **This is the doctrinal phase** — requires operator input on which roles the new brains hold.

#### Option A: Pure expansion (recommended)

Add 2 new roles + 2 new brains:

```python
ROLE_ANCHORS: dict[str, str] = {
    # Existing — pinned per pass #8 doctrine
    "strategist":          "alpha",
    "executor":            "camaro",
    "governor":            "chevelle",
    "opponent":            "redeye",
    "memory":              "shelly",
    # Phase 3 expansion (operator approval required):
    "crypto_strategist":   "mustang",      # ← new
    "crypto_opponent":     "corvette",     # ← new
}

LIVE_RUNTIMES = (
    "alpha", "camaro", "chevelle", "redeye",
    "mustang", "corvette",                # ← new
)
```

**Implications**:
- Crypto lane gets dedicated strategy + opponent voices
- Equity executor (Camaro) still routes both lanes (per existing seat model)
- Operator-tunable via runtime token assignment

#### Option B: Lane-specific duplication

Convert roles into `(lane, role)` tuples:

```python
ROLE_ANCHORS: dict[tuple[str, str], str] = {
    ("equity", "strategist"): "alpha",
    ("equity", "executor"):   "camaro",
    ("equity", "governor"):   "chevelle",
    ("equity", "opponent"):   "redeye",
    ("crypto", "strategist"): "mustang",
    ("crypto", "executor"):   "camaro",       # shared with equity
    ("crypto", "governor"):   "chevelle",     # shared with equity
    ("crypto", "opponent"):   "corvette",
}
```

**Implications**: more flexible but invasive — every consumer of `ROLE_ANCHORS` (15+ call sites) needs updating to pass a lane.

#### Option C: Pool model (most flexible, most invasive)

Remove `ROLE_ANCHORS` entirely. Roles are assigned to any brain via the existing seat-assignment endpoint. Brains have NO default role.

**Not recommended** — invalidates 8 passes of doctrine work.

**Recommended decision**: **Option A**. Cleanest, smallest blast radius, preserves doctrine.

**Estimated effort** (Option A): 1-2 hours including the operator-decision wait.

---

### Phase 4 — Frontend per-brain UI expansion

**Goal**: Update Overview cards, scorecards grid, discussion filters to support N brains dynamically.

**Files & changes**:

1. **`src/lib/api.js`** — extend `RUNTIME_META`:
```javascript
export const RUNTIME_META = {
  alpha:    { color: "#60A5FA", label: "ALPHA" },
  camaro:   { color: "#FBBF24", label: "CAMARO" },
  chevelle: { color: "#10B981", label: "CHEVELLE" },
  redeye:   { color: "#EF4444", label: "REDEYE" },
  mustang:  { color: "#A78BFA", label: "MUSTANG" },     // ← new
  corvette: { color: "#FB923C", label: "CORVETTE" },    // ← new
};
```

2. **`src/pages/Overview.jsx`** — change the 4-up brain card grid to a dynamic map:
```javascript
// BEFORE
<AlphaCard /> <CamaroCard /> <ChevelleCard /> <RedeyeCard />

// AFTER
{LIVE_RUNTIMES.map((rt) => <BrainCard key={rt} runtime={rt} {...} />)}
```

3. **`src/pages/Discussion.jsx`** — filter chips loop over `LIVE_RUNTIMES`
4. **`src/pages/Scorecards.jsx`** — scorecard grid loops over `LIVE_RUNTIMES`
5. **`src/components/LivePulse.jsx`** — already dynamic; no change needed
6. **`src/components/ParadoxRosterPanel.jsx`** — display new role anchors
7. **`src/components/RosterPanel.jsx`** — add row for new assignable roles

**New shared util** — `src/lib/runtimes.js`:
```javascript
// Fetched from /api/admin/runtimes (new endpoint) on app boot
// so frontend doesn't hardcode the brain list either
export let LIVE_RUNTIMES = ["alpha", "camaro", "chevelle", "redeye"];
export async function loadRuntimes(api) {
  const r = await api.get("/admin/runtimes");
  LIVE_RUNTIMES = r.data.runtimes;
}
```

**New backend endpoint** — `GET /api/admin/runtimes`:
```python
@router.get("/admin/runtimes")
async def list_runtimes(_user: dict = Depends(get_current_user)):
    """Single source of truth for the frontend. Returns the live
    brain roster + role anchors + per-brain metadata."""
    return {
        "runtimes": list(LIVE_RUNTIMES),
        "role_anchors": ROLE_ANCHORS,
        "runtime_meta": {
            rt: {"label": rt.upper(), "color": _color_for(rt)}
            for rt in LIVE_RUNTIMES
        },
    }
```

**Verification**:
- Visit Overview — 6 brain cards render
- Visit Discussion — 6 filter chips
- Visit Scorecards — 6 scorecard rows
- Smoke screenshot

**Estimated effort**: 3-4 hours.

---

### Phase 5 — Sidecar onboarding playbook (no code, operator runbook)

**Goal**: Document how the operator (or brain team) onboards the 2 new sidecars on PROD.

**Onboarding steps** (write as `/app/memory/ONBOARDING_NEW_BRAIN.md`):

1. **Pick brain name** — added to `LIVE_RUNTIMES` in `namespaces.py`
2. **Pick role anchor** — added to `ROLE_ANCHORS`
3. **Generate runtime token** — operator generates a unique `<BRAIN>_INGEST_TOKEN` env var on PROD
4. **Sidecar repo provisioned** — brain team builds a sidecar following the existing 4-brain pattern (heartbeat + sovereign contribution + intent emission + opinion posting)
5. **Smoke test** — operator hits `/api/admin/sidecar-diagnostics` on PROD and verifies the new brain shows `verdict: connected` within 5 minutes
6. **Pattern Watch readiness** — confirm the new brain pulls technical feeds for symbols in its lane
7. **Council inclusion** — verify cross-brain opinions include the new brain in `participants` arrays

**Per-brain README template** for sidecar teams:
- Required env vars (`MC_BASE_URL`, `<BRAIN>_INGEST_TOKEN`)
- Heartbeat cadence (every 60s to `/api/heartbeat-ping/<brain>`)
- Sovereign contribution cadence (every 60s to `/api/runtime-discussion/sovereign/contribution`)
- Intent emission via `/api/intents` with `X-Runtime-Token`
- Opinion posting via `/api/opinions` with `X-Runtime-Token`
- Position emission via `/api/positions` with `X-Runtime-Token`

**Estimated effort**: 1 hour.

---

## 4. Doctrine pins — what MUST stay invariant

These cannot drift across the refactor:

1. **Each brain has exactly ONE role anchor.** Adding a brain means adding a role. No "Camaro can be either executor or strategist depending on the day" — that's the pool-model trap (Option C above). Stay strict.

2. **Authority lives on the SEAT, not the brain identity.** This is already enforced (pass #8 — `LIVE EXEC` decoupled from promotion ladder). New brains may hold any seat the operator assigns; the SEAT carries doctrine.

3. **The role anchor table is operator-locked.** Adding a brain is a deploy event. Cannot be done from the dashboard at runtime. (Anti-misclick safety.)

4. **Existing 4 brains keep their existing role anchors.** Alpha = strategist forever; Camaro = executor forever. Adding new brains must NEVER reshuffle existing anchors.

5. **Brain identity cannot grant execution authority.** Doctrine (c). The Pydantic validator change must not touch any `may_execute` field handling.

6. **DB migration: NONE.** Existing rows in `shared_intents`, `sovereign_audit_log`, etc. keep working unchanged. The brain identity column already accepts any string; we're just expanding the validator vocabulary.

7. **Backwards-compat with 4-brain sidecars.** Adding `mustang` and `corvette` must not break Alpha / Camaro / Chevelle / REDEYE sidecars on PROD. Their tokens, endpoints, schemas stay identical.

---

## 5. Tripwires the implementer must add

| Phase | Test | Locks |
|---|---|---|
| 1 | `test_runtime_validator_accepts_all_live_runtimes` | Every entry in `LIVE_RUNTIMES` passes validation |
| 1 | `test_runtime_validator_rejects_unknown` | `"foo"` raises ValidationError |
| 1 | `test_runtime_validator_normalizes_case` | `"ALPHA"` → `"alpha"` |
| 1 | `test_no_literal_brain_tuple_remains_in_source` | Grep test — fails if any source file still has the 4-brain `Literal[...]` |
| 2 | `test_all_brain_iteration_uses_live_runtimes` | Grep test — fails if any test file still has `("alpha", "camaro", "chevelle", "redeye")` hardcoded |
| 3 | `test_role_anchors_contain_all_live_runtimes` | Every brain in `LIVE_RUNTIMES` has a role anchor |
| 3 | `test_existing_role_anchors_unchanged` | Alpha=strategist, Camaro=executor, Chevelle=governor, REDEYE=opponent — locked |
| 3 | `test_new_brain_endpoints_exist` | Each new brain has heartbeat-ping + sovereign-contribution + intent endpoints |
| 4 | `test_admin_runtimes_endpoint_returns_full_roster` | Frontend gets the canonical brain list from one curl |
| 4 | `test_frontend_runtime_meta_covers_all_live_runtimes` | Every brain has color + label |
| 5 | `test_brain_diagnostics_includes_all_live_runtimes` | `/admin/sidecar-diagnostics` returns N rows, not 4 |

---

## 6. Rollback plan

If anything goes wrong mid-refactor:

**Per-phase rollback**:
- **Phase 1**: revert the validator helper + restore `Literal[...]` constraints. Tripwires from before phase 1 must still pass.
- **Phase 2**: revert test fixture changes. Tests still iterate over the original tuple.
- **Phase 3**: revert `LIVE_RUNTIMES` tuple back to 4 + `ROLE_ANCHORS` back to original 5. New brains' sidecars start rejecting auth (no row in tokens), which is failsafe.
- **Phase 4**: revert frontend imports. UI shows 4-brain layout.
- **Phase 5**: nothing to revert (documentation only).

**Full rollback**: each phase commits as its own changeset so a single git revert undoes that phase.

---

## 7. Operational considerations

### 7a. Runtime token management

Each new brain needs a unique token. Recommended naming:
```
MUSTANG_INGEST_TOKEN=<32-char-hex>
CORVETTE_INGEST_TOKEN=<32-char-hex>
```

Generated via `python -c "import secrets; print(secrets.token_hex(32))"`.

### 7b. Seat-assignment grid expansion

The existing roster UI shows 4 brain × 4 role grid. Phase 4 expands to N × M. Render path:

```jsx
{LIVE_RUNTIMES.map(brain => (
  <Row brain={brain}>
    {SEAT_TYPES.map(seat => (
      <SeatCell brain={brain} seat={seat} />
    ))}
  </Row>
))}
```

### 7c. Conflict-detection cardinality

Cross-brain conflict detection runs O(N²) on opinion pairs per symbol. Going from 4→6 brains is 6 pair comparisons → 15 — manageable. At 10+ brains, the council aggregator may need batching.

### 7d. Live trading authority

New brains do NOT automatically get the executor seat. Operator must explicitly assign via the roster UI. Doctrine (c) enforced — new brains start as observers.

### 7e. Storage rollup impact

The 60-day rollup compactions in `shared/storage_rollup/` are keyed on `brain`. Adding brains means more rollup keys but no schema change. Verified safe.

---

## 8. Recommended execution order

**For maximum safety, do phases in this order:**

1. **Phase 1** (validator centralization) → all 480 tripwires must still pass
2. **Phase 2** (test fixture migration) → all 480 tripwires use `LIVE_RUNTIMES`
3. **Operator decision point** → which role anchors, which brain names
4. **Phase 3** (role anchor expansion) → 480 tripwires pass + new tripwires added
5. **Phase 4** (frontend) → smoke screenshot shows 6 brain cards
6. **Phase 5** (onboarding playbook) → handed to brain teams

**Do NOT** ship phases 1-4 separately — they should ride together in one deploy. Otherwise the frontend would show 6 brain cards while the backend only accepts 4 brains' intents, or vice versa.

---

## 9. Open questions for the next implementer

1. **Brain name choices?** Operator must pick (suggested American muscle car names: `mustang`, `corvette`, `barracuda`, `firebird`, `challenger`, `gto`, `dart`)
2. **Role assignments?** Option A (crypto_strategist + crypto_opponent) vs Option B vs Option C?
3. **Frontend color palette?** Already have 4 distinct colors; need to ensure new colors are accessible (WCAG AA contrast on dark background) and don't collide with existing palette
4. **Onboarding cadence?** Add both new brains at once, or stagger (mustang in Phase 3a, corvette in Phase 3b)? Recommended: **both at once** — same deploy, same tripwire run
5. **Sidecar repos?** Are brain teams ready to build sidecars for the new brains, or are mustang/corvette just placeholders for future expansion? Affects Phase 5 urgency.

---

## 10. References

- `/app/backend/namespaces.py:418-482` — `RUNTIMES`, `ROLE_ANCHORS`, `LIVE_RUNTIMES` definitions
- `/app/backend/shared/roster.py` — brain-roster CRUD endpoints
- `/app/backend/shared/intents.py` — `IntentIn.stack` field
- `/app/backend/shared/opinions.py` — `OpinionIn.runtime` field
- `/app/memory/CHANGELOG.md` — pass history (especially pass #8 single-sign promotion + seat doctrine)
- `/app/memory/PRD.md` — original problem statement + Doctrine (c)
- `/app/memory/DATA_STACK_PLAN.md` — companion plan (market data stack)

---

## 11. Decision checkpoint

**Before any code is written, get these answers from the operator:**

| Decision | Options | Default if no answer |
|---|---|---|
| New brain names | (operator picks 2) | `mustang`, `corvette` |
| Role assignment model | A / B / C | **A** (pure expansion) |
| New role names | (operator picks) | `crypto_strategist`, `crypto_opponent` |
| Stagger or batch deploy | one-shot / staged | **one-shot** |
| Sidecar timing | now / future | Future (placeholders for now) |
