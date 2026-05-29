# Response to Camaro-author re: multi-symptom degradation on prod (post iter-106z13)

**TL;DR**: Three independent regressions on prod simultaneously. (1) Sidecar env stamp invalid → `UNKNOWN_GIT_SHA`, every intent shunting to `dry_run_blocked`. (2) Heartbeat-ping has gone silent (5+ hours stale) despite the iter-106z12 patch. (3) Opinion POSTs dropped from 500+/24h to 0/24h — total regression on the per-intent loop that was your strongest channel last week. Each has a distinct fix. Code paths still work (1,351 intents emitted in 24h); something specific broke in the deploy.

---

## Live evidence (pulled from `mission.risedual.ai` just now)

```
GET /api/admin/brain/emission-diagnose/camaro
```

```json
{
    "summary": "camaro sidecar heartbeat is dead — process crashed or pod gone.",
    "silent_reasons": [
        "HEARTBEAT_DEAD",
        "SIDECAR_CHECKIN_INVALID",
        "NO_EXECUTOR_SEAT_FOR_LANE"
    ],
    "heartbeat": {
        "heartbeat_age_seconds": 18949.6,
        "heartbeat_band": "dead",
        "ever_heartbeated": true,
        "intents_last_24h": 1351,
        "opinions_last_24h": 0,
        "signals": {
            "heartbeat_fresh": false,
            "intent_recent": false,
            "opinion_recent": false
        }
    },
    "sidecar_checkin": {
        "verdict": "invalid",
        "errors": ["UNKNOWN_GIT_SHA"],
        "policy_hash_match": true,
        "checkin_age_seconds": 268.6,
        "ever_checked_in": true
    },
    "roster": {
        "seats_held": ["strategist", "crypto_strategist"],
        "holds_equity_executor": false,
        "holds_crypto_executor": false
    },
    "emission": {
        "total_intents_ever": 17824,
        "window_total": 1351,
        "by_action": { "BUY": 22, "SELL": 18, "HOLD": 1311 },
        "by_lane": { "equity": 836, "crypto": 515 },
        "by_gate_state": {
            "pending": 11,
            "passed": 0,
            "blocked": 7,
            "dry_run_passed": 0,
            "dry_run_blocked": 1332
        }
    }
}
```

The internal contradiction is the diagnostic clue: **checkin is 268s old (fresh)** but **heartbeat is 18,949s old (5h dead)**. Process is alive enough to checkin and to emit 1,351 intents in 24h, but the heartbeat-ping cron specifically isn't firing. That points to one of: heartbeat scheduler crashed silently, the heartbeat-ping URL got mis-templated in the post-iter-106z12 deploy, or you're sending heartbeat to the wrong path again (e.g. reverted to legacy `POST /api/ingest/heartbeat`).

---

## Three separate problems, three separate fixes

### Problem 1 — `UNKNOWN_GIT_SHA` env stamp invalid

Same family as the Alpha and RedEye sidecar-stamp issues. MC's `validate_for_prod_sidecar` rejects `git_sha == "" / "unknown"`. Fix: set `GIT_SHA` (or `VERCEL_GIT_COMMIT_SHA`) at deploy time on your prod pod.

| Field | Required | Env var |
|---|---|---|
| `env_name` | `"prod"` | `RISEDUAL_ENV` |
| `mc_url` | starts with `https://mission.risedual.ai` | `RISEDUAL_MC_URL` |
| `db_name` | NOT in `("","preview","test","unknown")` | `RISEDUAL_DB_NAME` |
| `broker_mode` | `"paper" / "live" / "dry_run"` | `RISEDUAL_BROKER_MODE` |
| `git_sha` | NOT `"" / "unknown"` | `GIT_SHA` ← **this is your bad field** |
| `local_execution_authority` | `False` | hard-coded |

Pull your platform's commit-sha env var (Vercel: `VERCEL_GIT_COMMIT_SHA`; many others: `GIT_SHA` or `COMMIT_SHA`) into your sidecar's env stamp builder. If your CI doesn't inject it, set it explicitly in your deploy script before the sidecar boots:

```bash
export GIT_SHA=$(git rev-parse --short HEAD)
```

### Problem 2 — Heartbeat-ping silent for 5+ hours

You were the brain that landed iter-106z12's heartbeat-channel fix — `GET /api/heartbeat-ping/camaro?token=...`. Something has stopped calling it on prod. Verify:

```bash
# Manual smoke from your sidecar pod
curl -v "https://mission.risedual.ai/api/heartbeat-ping/camaro?token=$CAMARO_INGEST_TOKEN"
```

Expected: `200 OK` with a JSON body. If that works, your cron is broken (or the URL template was re-broken in the latest deploy). If it doesn't work, the token rotated or the URL drifted.

The "checkin is fresh but heartbeat is dead" pattern strongly suggests these two channels were wired to different schedulers and only one is still running. Audit both schedulers, share a deploy timestamp, and check which scheduler died.

### Problem 3 — Opinion POSTs dropped from 500+ to 0 in 24h

This is the biggest regression. Last week you were posting 500+ opinions per 24h — the gold-standard cadence the other brain teams were copying. Last 24h: **zero**. The 504 work in iter-106z12 included MC-side latency guards on `/api/ingest/opinion` (pass #22, MC commit 2026-05-28). Bounded with `asyncio.wait_for` for both anchor fetch (1.5s) and conflict-detect (2.0s); opinion always lands within the 10s ingress budget.

If your sidecar's outbound HTTP client is treating MC's *opinion* posts as "non-essential" and dropping them under backpressure, that would explain a silent zero. Three things to check:

1. Outbound HTTP retry budget per call type. Make sure opinions aren't being deprioritized vs intents.
2. Are you hitting a stricter timeout on your side that fires BEFORE MC's response? MC now responds within ~200ms typical, ~3.5s p99. If you're aborting at 1s, you might be killing the call mid-response.
3. Did the iter-106z12 work change a `_now_iso()` helper or a token-attaching middleware in a way that broke ONLY the opinions code path? `submit_intent_v2_to_mc` works (1,351 in 24h); `post_opinion` does not.

Smoke test:

```bash
curl -s -X POST https://mission.risedual.ai/api/ingest/opinion \
  -H "X-Runtime-Token: $CAMARO_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "runtime": "camaro",
    "topic": "symbol:TEST",
    "stance": "observation",
    "confidence": 0.5,
    "body": "camaro smoke test post-iter-106z13",
    "evidence": {},
    "may_execute": false
  }'
```

Expected: `200` with `{"ok": true, "opinion_id": "<uuid>"}`. If that works from your pod's network namespace, the wire is fine and your in-process code path is broken — diff `submit_intent_v2_to_mc` vs `post_opinion_v2_to_mc` looking for the divergence.

---

## What we expect to see once all three are fixed

1. `verdict: prod`, `errors: []` on the sidecar checkin → intents stop force-shunting to `dry_run_blocked`. With your strategist seats, expect `dry_run_passed` to start climbing for non-HOLD intents.
2. `heartbeat_age_seconds < 60`, `heartbeat_band: "fresh"` → operator dashboard flips Camaro back from "DEAD" to active.
3. `opinions_last_24h` climbs back toward 500+ as your per-intent loop resumes.
4. `silent_reasons` shrinks to `["NO_EXECUTOR_SEAT_FOR_LANE"]` — operator-side, not yours.

---

## Notes on your seats

```json
"seats_held": ["strategist", "crypto_strategist"]
```

You currently hold equity strategist and crypto strategist — the trade-thesis seats on both lanes.

**Doctrine pin (from `shared/roster.py` line 111):**
> IDENTITY DOES NOT GRANT AUTHORITY. SEAT POLICY DOES.

By default **every brain is eligible for every seat.** The operator can rotate Camaro into any role — strategist, executor, auditor — at any time, on either lane. The ONLY carve-out in the codebase is a seat-side restriction on `governor` and `crypto_governor`, which by default only Chevelle and RedEye satisfy (and even that is a seat-level toggle, not a permanent identity property). Everything else is fluid.

Build your sidecar to handle any seat assignment it receives from MC, not just the strategist work you happen to be doing today. The roster broadcast tells you which seat(s) you hold; your code should branch on that, not assume.

The high HOLD ratio in your current data (1311 HOLD vs 22 BUY / 18 SELL / 0 SHORT) reflects strategist conviction floor working as designed — when you don't have a high-conviction trade, you correctly emit HOLD. That's the right behavior. The problem isn't your thesis output; it's that none of your non-HOLD intents are reaching live execution because of Problems 1–3 above.

---

## Doctrine pin

MC enforces the env stamp because broker keys live on production MC, not on the sidecar pod. The heartbeat is a liveness signal — a dead heartbeat tells the operator dashboard not to trust this brain even if it's emitting. The opinion-post loop is the cross-brain discussion layer — silence here means MC's other gates fall back to deterministic doctrine. Three separate signals, three separate doctrines, all currently red on Camaro.

The pleasant surprise: your intent-emission path is still healthy. 1,351 intents/24h means the strategist heuristic is running. Once the three signal channels are restored, the rest of the gate chain should flow.
