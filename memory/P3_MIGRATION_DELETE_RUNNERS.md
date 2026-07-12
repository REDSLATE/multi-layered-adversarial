# P3 Migration — Delete Legacy Runners (operator-initiated)

_Written: 2026-07-12 (iter-28c). Prerequisite: P2 shipped — all 4 pulse brains registered and writing to `mc_opinions_compare`._

## Doctrine gate (must ALL hold across 24h+ observation)

Per `MC_PULSE.md §11`, the arbiter cannot flip to pulse-only until every brain shows sustained gates-pass:

| Metric | Threshold | Read from |
|---|---|---|
| `match_score` | ≥ 0.60 | `GET /api/mc/parity/{brain}/history` |
| `pulse_confidence_std` | > 0.02 | same |
| `pairs_matched` | ≥ 20 | same |

All three must be `True` for **every 15-min snapshot across a full trading session per lane** (equity RTH ≥ 6.5h; crypto 24h). A single-snapshot pass is NOT sufficient — noise can cross gates transiently.

**Operator quick check:**

```bash
API_URL=$(grep REACT_APP_BACKEND_URL /app/frontend/.env | cut -d '=' -f2)
TOKEN=<admin token>
for b in camino gto barracuda hellcat; do
  echo "=== $b ==="
  curl -s "$API_URL/api/mc/parity/$b/history?limit=96" \
    -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);s=d['snapshots'];print(f'  snapshots={len(s)}, gates_pass_rate={sum(1 for r in s if r[\"arbiter_flip_gates_pass\"])/max(len(s),1):.2%}')"
done
```

Every brain should show `gates_pass_rate=100%` (96 snapshots = 24h at 15min cadence).

## Step-by-step deletion

### 1. Flip the kill switch (reversible)

```bash
echo 'RISEDUAL_LEGACY_RUNNERS_ENABLED=false' >> /app/backend/.env
sudo supervisorctl restart backend
```

Verify:

```bash
sleep 10
grep "legacy runners DISABLED" /var/log/supervisor/backend.out.log | tail -1
```

Should show: `legacy runners DISABLED (RISEDUAL_LEGACY_RUNNERS_ENABLED=false)`.

### 2. Observation window (minimum 1 full session per lane)

- **Equity**: 1 full RTH (9:30 ET → 16:00 ET, ~6.5h).
- **Crypto**: 24h continuous (no session boundary).

During this window:
- **Pulse SHOULD keep writing**: `mc_opinions_compare` count continues to climb.
- **Runner SHOULD stop writing**: `shared_intents` should show NO new rows with `stack ∈ {camino, gto, barracuda, hellcat}`. Any new `shared_intents` writes are from the operator UI or another surface — NOT the runners.
- **Backend logs SHOULD be silent** on `neutral_brains` — no start/stop/heartbeat entries.

Verification queries:

```python
# in a pod shell
import asyncio, os
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

async def check():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    # runners must be silent
    async for d in db.shared_intents.aggregate([
        {"$match": {"ingest_ts": {"$gte": cutoff}}},
        {"$group": {"_id": "$stack", "n": {"$sum":1}}},
    ]):
        print(f"[shared_intents 1h] stack={d['_id']} n={d['n']}  <-- should be 0 for brain stacks")

    # pulse must be alive
    async for d in db.mc_opinions_compare.aggregate([
        {"$match": {"evaluated_at": {"$gte": cutoff}}},
        {"$group": {"_id": "$brain", "n": {"$sum":1}}},
    ]):
        print(f"[mc_opinions_compare 1h] brain={d['_id']} n={d['n']}  <-- should be nonzero")

asyncio.run(check())
```

### 3. Delete legacy code + supervisor plumbing

Once the observation window closes clean:

```bash
# Static grep: confirm nothing else imports from external.brains
grep -rn "external.brains\|external/brains" /app/backend --include="*.py" \
  | grep -v __pycache__ | grep -v test_

# The audit checklist (MC_PULSE.md §8) must show ALL these callers gone:
#   - server_modules/lifespan.py::start_neutral_brains
#   - shared/snapshot_enrich/equity_doctrine.py
#   - routes/brain_runtime.py
#   - server_modules/meta_routes.py
#   - shared/runtime/sidecar_checkin.py
#   - routes/data_stack_admin.py

# Then delete:
rm -rf /app/external/brains
# (leave /app/external in case other future submodules land there)

# Verify backend still boots
sudo supervisorctl restart backend && sleep 10
sudo supervisorctl status backend  # RUNNING

# Verify pulse still writes
curl -s "$API_URL/api/mc/parity/camino?hours=1" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('pulse_count',d['pulse_count'])"
# Should be > 0. runner_count will be 0 (correct — runners gone).
```

### 4. Update PARITY_SNAPSHOT_BRAINS gate criteria (optional)

Once runners are gone, `match_score` and `timestamp_drift_median_s` become meaningless (no runner tape to compare against). The snapshotter continues to write `pulse_count`, `pulse_confidence_std`, `pulse_confidence_mean` — those stay meaningful.

Consider adding a `POST_RUNNERS` flag to `take_parity_snapshot` that:
- Skips runner-dependent metrics.
- Sets `arbiter_flip_gates_pass=None` (contract no longer applies — the flip already happened).
- Continues to record pulse health.

This is an optional cleanup — the current code fail-softs when runner_count=0.

## Rollback path (if any of the above goes wrong)

At every step BEFORE the `rm -rf`:

```bash
# Re-enable runners
sed -i '/RISEDUAL_LEGACY_RUNNERS_ENABLED=false/d' /app/backend/.env
sudo supervisorctl restart backend
```

After `rm -rf`, rollback requires:
1. `git checkout HEAD -- external/brains/` (if using Emergent's git checkpoints).
2. `sed -i '/RISEDUAL_LEGACY_RUNNERS_ENABLED=false/d' /app/backend/.env`
3. `sudo supervisorctl restart backend`

## Files that will be affected

**Deleted:**
- `/app/external/brains/runner.py` (~2,300 lines)
- `/app/external/brains/brain_core.py` (used by pulse brains via `mc_brains/_pulse_base.py` — MUST relocate first, see below)
- `/app/external/brains/personality.py` (also used by `_pulse_base.py` — MUST relocate first)

**Must relocate before deletion:**
- `brain_core.py` and `personality.py` are imported by `mc_brains/_pulse_base.py`. Move them into `/app/backend/mc_brains/_legacy/` or `/app/backend/shared/brain_core/` before rm.

Suggested pre-deletion move:

```bash
mkdir -p /app/backend/mc_brains/_legacy
mv /app/external/brains/brain_core.py /app/backend/mc_brains/_legacy/brain_core.py
mv /app/external/brains/personality.py /app/backend/mc_brains/_legacy/personality.py
# Update the 2 imports in mc_brains/_pulse_base.py:
#   from external.brains.brain_core → from mc_brains._legacy.brain_core
#   from external.brains.personality → from mc_brains._legacy.personality
```

After that, `rm -rf /app/external/brains` deletes only the runner + `__init__.py`.

## Sign-off checklist

Copy-paste this into an audit row before firing the `rm -rf`:

- [ ] All 4 brains show `gates_pass_rate=100%` across ≥96 snapshots (24h) for both lanes
- [ ] `RISEDUAL_LEGACY_RUNNERS_ENABLED=false` flipped, backend restarted
- [ ] 1 full RTH (equity) + 24h (crypto) observation window closed
- [ ] `shared_intents` shows 0 new brain-stack writes across the window
- [ ] `mc_opinions_compare` shows nonzero writes across the window for all 4 brains
- [ ] `grep external.brains /app/backend` returns 0 hits outside `mc_brains/_legacy/` and this doc
- [ ] `brain_core.py` + `personality.py` relocated to `mc_brains/_legacy/`
- [ ] `_pulse_base.py` imports updated to the new locations
- [ ] 216+ pulse test suite still green
- [ ] Backend restart post-deletion is clean (no import errors in `.err.log`)
- [ ] Operator has visually confirmed the last snapshot in Kernel Review / dashboard
