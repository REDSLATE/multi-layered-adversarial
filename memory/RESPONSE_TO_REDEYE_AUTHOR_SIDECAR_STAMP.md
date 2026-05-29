# Response to RedEye-author re: sidecar env stamp invalid on prod

**TL;DR**: RedEye's sidecar checkin verdict on prod is `preview` with error `ENV_NOT_PROD`. MC's gate chain force-shunts every intent to `dry_run_blocked` because the stamp says you're a preview-environment sidecar. 158 intents emitted in the last 24h, 0 reached `passed`. This is a 1-line env-var fix, not a code change.

---

## Live evidence (pulled from `mission.risedual.ai` just now)

```
GET /api/admin/brain/emission-diagnose/redeye
```

```json
{
    "summary": "redeye status: SIDECAR_RUNNING_IN_PREVIEW, RECENT_DIRECTIONAL_PRESENT",
    "silent_reasons": ["SIDECAR_RUNNING_IN_PREVIEW", "RECENT_DIRECTIONAL_PRESENT"],
    "sidecar_checkin": {
        "verdict": "preview",
        "errors": ["ENV_NOT_PROD"],
        "policy_hash_match": true,
        "ever_checked_in": true
    },
    "roster": {
        "seats_held": ["executor", "crypto_governor"],
        "holds_equity_executor": true
    },
    "emission": {
        "total_intents_ever": 762,
        "window_total": 158,
        "by_action": { "BUY": 92, "SELL": 2, "HOLD": 64 },
        "by_lane":   { "equity": 54, "crypto": 104 },
        "by_gate_state": {
            "pending": 0,
            "passed": 0,
            "blocked": 3,
            "dry_run_blocked": 155,
            "dry_run_passed": 0,
            "rejected_at_ingest": 0
        }
    },
    "heartbeat": {
        "liveness": "active",
        "heartbeat_age_seconds": 7.1,
        "intents_last_24h": 24
    }
}
```

Heartbeat is fresh (7s). `ever_checked_in: true`. `policy_hash_match: true`. **You hold the equity executor seat.** The sidecar IS reaching MC and the policy is in sync. **Only the env stamp is wrong** — and that single failure puts every intent on `dry_run_blocked`. The gate chain runs, the decision IS made, the decision is "do not execute, this sidecar is preview."

---

## What MC actually checks

`shared/runtime/platform_survival.py::validate_for_prod_sidecar`. All six must be true:

| Field | Required value | Env var |
|---|---|---|
| `env_name` | literal string `"prod"` | `RISEDUAL_ENV` (or `ENV` fallback) |
| `mc_url` | starts with `https://mission.risedual.ai` | `RISEDUAL_MC_URL` |
| `db_name` | NOT in `("", "preview", "test", "unknown")` | `RISEDUAL_DB_NAME` |
| `broker_mode` | one of `"paper"`, `"live"`, `"dry_run"` | `RISEDUAL_BROKER_MODE` |
| `git_sha` | not `""` / `"unknown"` | `GIT_SHA` or `VERCEL_GIT_COMMIT_SHA` |
| `local_execution_authority` | `False` (hard-coded) | — |

Yours is currently failing **`ENV_NOT_PROD`** — your sidecar is stamping something other than the literal string `"prod"` for `env_name`. Common causes: `RISEDUAL_ENV` unset (defaults to `"unknown"`), set to `"preview"`, `"production"` (note: must be lowercase `"prod"`, not `"production"`), or stamped from a different env-var your sidecar code reads instead of `RISEDUAL_ENV`/`ENV`.

---

## What to do

1. **Set the env on your prod sidecar pod**:

   ```bash
   RISEDUAL_ENV=prod                              # ← THE FIX — must be literal "prod"
   RISEDUAL_MC_URL=https://mission.risedual.ai
   RISEDUAL_DB_NAME=<your real prod mongo db name>     # NOT "preview" / "test" / blank
   RISEDUAL_BROKER_MODE=paper                          # or "live" / "dry_run"
   GIT_SHA=<commit sha at deploy>
   RISEDUAL_PLATFORM=<your hosting platform name>
   RISEDUAL_SIDECAR_VERSION=<your version tag>
   RISEDUAL_APP_NAME=redeye
   ```

2. **Redeploy** so the sidecar starts re-stamping with the new values.

3. **Verify** by asking the operator to run:

   ```bash
   curl -s "https://mission.risedual.ai/api/admin/brain/emission-diagnose/redeye" \
     -H "Authorization: Bearer <operator-jwt>" | jq '.sidecar_checkin'
   ```

   Expected: `"verdict": "prod"`, `"errors": []`. Anything else means the stamp still isn't clean — read the error code, match it back to the table above, fix that env var. This endpoint requires an operator JWT; brain teams cannot hit it directly, so coordinate with the operator for a one-off curl after redeploy.

---

## What changes once the stamp goes green

Your intents will start landing at **`dry_run_passed`** instead of `dry_run_blocked`. That's the next step on the staircase, not the final one:

```
Brain emits intent
  → MC ingest stamps stack/seat
  → AUTO_DRY_RUN_ON_INGEST hook fires _evaluate_gates within ~50ms
  → currently: dry_run_blocked  (env stamp invalid → force-shunt)
  → after fix:  dry_run_passed  (gate chain says "would pass live")
  → real execution: requires /execution/submit being called by the
                    operator OR the auto-router promoting the intent
```

The `dry_run_*` is a dress rehearsal. Promotion to a real `passed` state (and an actual broker order) requires the live execution path firing — that's the auto-router's job, not yours. Once you're at `dry_run_passed`, the rest is operator-controlled.

---

## How to know it's fixed (three signals, in order)

1. `verdict: prod` on the sidecar checkin diagnose
2. `silent_reasons` drops `SIDECAR_RUNNING_IN_PREVIEW`
3. `by_gate_state.dry_run_passed > 0` and `dry_run_blocked` stops growing — those are intents the gate chain WOULD have executed had the operator or auto-router promoted them

---

## Notes on your seats (informational, but action needed on the governor side)

```json
"seats_held": ["executor", "crypto_governor"]
```

**Doctrine pin (from `shared/roster.py` line 111):**
> IDENTITY DOES NOT GRANT AUTHORITY. SEAT POLICY DOES.

By default every brain is eligible for every seat. The ONLY carve-out in the codebase is a seat-side restriction on `governor` and `crypto_governor`, which by default only Chevelle and RedEye satisfy. That's a property of the **seat's eligibility toggle**, not a permanent identity property of the brain — the operator owns it. The operator can also tighten any non-governor cell on any brain at any time.

What that means for you: the operator can rotate you into ANY seat — strategist, executor, governor, auditor — on either lane, at any time. Build your sidecar to branch on the seat(s) the roster broadcast says you hold, not on a hard-coded assumption about your role.

Concrete behaviors per seat you might hold:

- equity / crypto `strategist` → form trust/reduce/veto/observation theses on intents in that lane
- equity / crypto `executor` → emit intents to `POST /api/intents` (your current equity seat)
- equity / crypto `governor` → post `evidence.authority_call` on every opinion for that lane (your current crypto seat)
- equity / crypto `auditor` → post pre-trade contrary case + post-trade review opinions

**Your `crypto_governor` seat today.** Governance authority on the crypto lane. Role: post `evidence.authority_call` on EVERY opinion you submit for a crypto symbol. The crypto governor gate (`_build_governor_gate` → `_latest_governor_call`) reads `shared_adl_receipts` looking for your most recent authority_call. If it doesn't find one within freshness window, the gate marks the call as silent and the intent's risk multiplier drops (or hard-blocks if it's a fatal taxonomy code like `GOVERNOR_HARD_VETO`).

Pattern documented in `RESPONSE_TO_CHEVELLE_AUTHOR_OPINIONS.md` — same shape, just swap `chevelle` → `redeye` and `lane=equity` → `lane=crypto`:

```json
"evidence": {
    "authority_call": {
        "brain": "redeye",
        "status": "ALLOW",            // ALLOW | WARN | BLOCK
        "reason": "NO_GOVERNOR_DISSENT",
        "confidence": 0.7,
        "symbol": "BTC",
        "lane": "crypto"
    }
}
```

`brain` MUST equal `runtime` — MC defensively refuses authority_calls that impersonate another brain. If the operator rotates you to equity `governor` tomorrow, the only thing that changes is `"lane": "equity"`.

**Equity `executor` seat today.** Full execution authority on equity. Once your env stamp is green, equity BUY/SHORT/SELL/COVER intents go through to `dry_run_passed`. No authority_call required for this seat — the intent itself IS the call.

---

## Doctrine pin

MC enforces the env stamp because broker keys live on production MC, not on the sidecar pod. If a `preview` or `test` sidecar's intents were allowed onto the live gate chain, a dev environment could trigger real orders. That's the doctrine the validator is protecting. The fix is in your `.env` — there is no MC-side workaround.
