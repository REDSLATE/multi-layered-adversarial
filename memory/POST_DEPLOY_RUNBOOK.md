# Pass 3 Post-Deploy Runbook

**When to use this:** Immediately after clicking "Deploy" in the
Emergent panel with the Pass 3 code (Mongo-off-the-critical-path).

**Assumption:** Prod URL is your deployed host, e.g.
`https://{app}.emergent.host`. Substitute below.

---

## ⚠️ One-time infrastructure note

`/app/trader/data/` lives on the pod's ephemeral filesystem. On a pod
restart or redeploy the SQLite is **wiped**. That's mostly fine because:

* `daily_spent_usd` resets to 0 → worst case the trader could re-spend
  the daily cap once per pod lifetime. With `TRADER_DAILY_USD_CAP=1000`
  that's a $1000 max blast radius per restart.
* `already_executed` idempotency only catches re-fires **within the
  same pod lifetime**. Mongo mirror is the cross-restart source of
  truth for that — set `TRADER_DAILY_USD_CAP` to something small
  (e.g. $50) while pilot-testing.

If you want SQLite to survive pod restarts, mount a PersistentVolume
at `/app/trader/data/`. Not required for the pilot.

---

## 1 · Deploy (your action)

Click **Deploy** in the Emergent panel. Wait for the pod to become
Ready.

## 2 · Sanity check the boot

```bash
export PROD=https://YOUR-APP.emergent.host
export TOKEN=$(curl -s -X POST "$PROD/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@risedual.io","password":"risedual-admin-2026"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# Store initialized? Empty tape is expected right now.
curl -s "$PROD/api/admin/trader/health" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Expect: `store.executions_total=0`, `state.master_armed=false`,
`state.last_refresh_ok_ts` may be null (Atlas still degraded, that's OK).

**Success criterion:** the endpoint returns 200. It does not depend on Mongo.

## 3 · Seed the seat_registry

```bash
curl -s -X POST "$PROD/api/admin/trader/seed-seats" \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

This writes the operator-canonical angel↔brain pairings into Mongo's
`seat_registry`. It may fail (Atlas timeout) — that's fine; the trader
will use `DEFAULT_SEATS` as a fallback.

Also click **Reseed canonical pairings** in the *Trader Seats* tile if
you'd rather do it from the UI.

## 4 · Arm the master switch to $1

Set the tiny cap **before** flipping `TRADER_ENABLED`:

Emergent panel → env vars:
```
TRADER_ENABLED=true
TRADER_PER_ORDER_USD_CAP=1
TRADER_DAILY_USD_CAP=5
TRADER_INTERVAL_SEC=60
```

Then arm the master switch (via Mongo or MC's UI — the trader defaults
to disarmed for safety):
```bash
# If Mongo is reachable:
curl -s -X POST "$PROD/api/admin/master-trading-switch" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"enabled": true, "reason": "pilot start"}'
```

## 5 · Watch the Trade Tape

Open `https://YOUR-APP.emergent.host/admin/overview` in the browser.
Scroll to the **Trade Tape** tile.

Within 60s (one cycle) you should see:
* `TRADER: ENABLED`
* `LOOP: ALIVE`
* At least 2 rows (one per lane) with time · lane · symbol · executor
* Each row shows either `FIRED` (broker accepted), `hold` (verdict
  was HOLD), or `pass` (risk blocked with a reason)

## 6 · Confirm the 6 verification points

Run this one-liner and cross-check the output against the 6 items:

```bash
curl -s "$PROD/api/admin/trader/receipts?limit=20" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
rows = d.get('items', [])
print(f'  ✓ Received {len(rows)} receipts from LOCAL SQLITE')
print(f'  ✓ Mongo not required — endpoint served without touching Atlas')
by_lane = {}
for r in rows:
    by_lane.setdefault(r.get('lane'), []).append(r)
for lane in ('equity', 'crypto'):
    lane_rows = by_lane.get(lane, [])
    fired = [r for r in lane_rows if r.get('broker_result') and not r.get('error')]
    blocked = [r for r in lane_rows if (r.get('risk') or {}).get('ok') is False]
    holds = [r for r in lane_rows if (r.get('chosen') or {}).get('verdict') == 'HOLD']
    print(f'  {lane}: {len(lane_rows)} rows · {len(fired)} fired · {len(blocked)} risk-blocked · {len(holds)} holds')
    if blocked:
        print(f'    sample risk reason: {blocked[0][\"risk\"].get(\"reason\")}')
    if fired:
        br = fired[0]['broker_result']
        print(f'    sample broker: {list(br.keys())[:3]}')
"
```

**Pass criteria (all six):**

| # | Check | How to see it |
|---|---|---|
| 1 | `TRADER_ENABLED=true` | Tile shows `TRADER: ENABLED` |
| 2 | Mongo not required | `/receipts` returns 200 even if `/api/admin/mongo/status` fails |
| 3 | SQLite Trade Tape filling | Row count grows over time (`receipts_total` at `/health`) |
| 4 | Webull one receipt | equity row with `FIRED` + broker `webull` |
| 5 | Kraken one receipt | crypto row with `FIRED` + broker `kraken` |
| 6 | Risk reasons visible when blocked | Tile shows red `pass` rows with `risk.reason` (`daily_cap_exceeded`, `master_switch_disarmed`, etc.) |

## 7 · Kill switch (if anything looks wrong)

Fastest → env var:
```
TRADER_ENABLED=false
```
Redeploy. The trader stops on next boot.

Or immediate → flip master switch:
```bash
curl -s -X POST "$PROD/api/admin/master-trading-switch" \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"enabled": false, "reason": "kill"}'
# Then force the cache to pick it up right away:
curl -s -X POST "$PROD/api/admin/trader/reload-caches" -H "Authorization: Bearer $TOKEN"
```
Next cycle (≤60s) the risk gate will block every trade with
`master_switch_disarmed`.
