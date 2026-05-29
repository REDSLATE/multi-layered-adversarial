# Response to Chevelle-author re: opinion-silent on prod (post iter-106z12 / 2026-05-28)

**TL;DR**: You're the #1 gate-chain blocker in production right now. Chevelle holds Equity Governor + Crypto Executor and is opinion-silent on both. Your sidecar's heartbeat + sovereign channels are healthy, but `POST /api/ingest/opinion` is firing **once a day** instead of **once per intent**. This message gives you the exact contract and the live evidence.

---

## Live evidence (pulled from `mission.risedual.ai` just now)

| Brain   | Last opinion        | Opinions in last 24h |
|---------|---------------------|----------------------|
| camaro  | 17 s ago            | 500+ (capped)        |
| redeye  | 1 s ago             | 500+ (capped)        |
| **chevelle** | **37 h ago**   | **0**                |
| alpha   | NEVER               | 0                    |

Chevelle's last opinion: `posted_at=2026-05-27T05:36:40Z`, `topic="free"`, `stance="observation"`. One post, ~37 hours ago, then silence. So your code path **works** — it's just not being called per intent.

---

## Why this stalls every equity trade

Equity Governor = Chevelle. When MC's gate chain hits `_latest_governor_call()` and finds no opinion-mirrored receipt for the symbol, it falls back to deterministic doctrine. The fallback writes a conservative `NO_GOVERNOR_DISSENT` / `GOVERNOR_HARD_VETO_*` record, which the executor reads as a block. Net effect: **every equity intent stalls.** Same blocking pattern applies to crypto via your Crypto Executor seat.

---

## What you need to do

Add a loop that calls `POST /api/ingest/opinion` **every time MC emits an intent that involves a symbol you have a seat for**. Not on a timer — **per intent**. Camaro and RedEye are doing this 500+ times in 24h; you're doing it 0.

To discover what intents need a verdict, poll:

```
GET https://mission.risedual.ai/api/runtime-discussion/opinions?caller=chevelle
Header:  X-Runtime-Token: <CHEVELLE_INGEST_TOKEN>
```

…or subscribe to whatever signal source Camaro is using. For each intent, post your verdict back via the contract below.

---

## Verified MC opinions contract

**Endpoint:** `POST /api/ingest/opinion`
**Auth header:** `X-Runtime-Token: <CHEVELLE_INGEST_TOKEN>`
**Body schema** (Pydantic-validated; off-spec = HTTP 422):

```json
{
    "runtime": "chevelle",
    "topic": "symbol:AAPL",
    "stance": "veto",
    "confidence": 0.78,
    "body": "Position size > sector cap; refuse.",
    "evidence": { "sector_exposure_pct": 0.31, "cap_pct": 0.25 },
    "in_reply_to": null,
    "regime": "high_vol",
    "may_execute": false
}
```

### Governor-specific guidance (since you hold the Governor seat)

For each intent, your verdict should land in one of these shapes:

| Decision           | `stance`       | When                                       |
|--------------------|----------------|--------------------------------------------|
| Allow              | `endorse`      | governance checks pass                     |
| Allow with warning | `observation`  | borderline; flag the issue but don't block |
| Soft block         | `disagree`     | reject this attempt; not a forever-no      |
| Hard veto          | `veto`         | governance line crossed (cap, freeze, etc.) |

You can ALSO mirror a governor authority_call into `evidence.authority_call` (MC's `_mirror_authority_call_to_receipts` will translate it into the receipts collection the gate chain reads from):

```json
"evidence": {
    "authority_call": {
        "brain": "chevelle",
        "status": "BLOCK",                 // ALLOW | WARN | BLOCK
        "reason": "GOVERNOR_HARD_VETO_SECTOR_CAP",
        "confidence": 0.78,
        "symbol": "AAPL",
        "lane": "equity"
    }
}
```

`brain` MUST equal `runtime` — MC defensively refuses authority_calls that impersonate another brain.

---

## Smoke test (run this once you ship)

```bash
curl -s -X POST https://mission.risedual.ai/api/ingest/opinion \
  -H "X-Runtime-Token: $CHEVELLE_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "runtime": "chevelle",
    "topic": "symbol:TEST",
    "stance": "observation",
    "confidence": 0.5,
    "body": "smoke test from chevelle sidecar",
    "evidence": {},
    "may_execute": false
  }'
```

Expected: `200` with `{"ok": true, "opinion_id": "<uuid>", ...}`. If you get 401 → token wrong. 422 → schema wrong. 200 once but no follow-up = your per-intent loop isn't wired.

---

## How to know it's fixed

Watch the operator dashboard's Seat Roster strip on prod. The `chevelle @ governor` and `chevelle @ crypto_executor` cells should flip from "1d ago" / red to a freshness measured in minutes, matching camaro and redeye. Once that happens, the gate chain unblocks and queued intents start flipping `gate_state: pending → passed`.

You can also ask the operator to run:
```
GET https://mission.risedual.ai/api/shared/opinions?runtime=chevelle&limit=5
Header: Authorization: Bearer <operator-jwt>
```
This endpoint requires an operator JWT — brain teams cannot hit it directly. Coordinate with the operator for a one-off check after redeploy.

---

## Build for any seat — you're governor-eligible

**Doctrine reminder.** Chevelle and RedEye are the **only two brains** in MC's roster who can hold ANY governor seat (equity `governor` OR `crypto_governor`). Alpha and Camaro are doctrinally excluded — the operator can rotate either of you into any governor seat at any time, on either lane. Build your sidecar to handle ALL of these placements, not just current ones:

- equity `strategist` → form trust/reduce/veto/observation calls on equity intents
- equity `governor` → post `evidence.authority_call` on equity opinions (your current equity seat)
- equity `executor` → emit equity intents
- equity `auditor` → post pre-trade contrary case + post-trade review opinions
- `crypto` (executor) → emit crypto intents (your current crypto seat)
- `crypto_governor` → post `evidence.authority_call` on crypto opinions
- `crypto_strategist` → form crypto trade theses
- `crypto_auditor` → post pre-trade contrary case + post-trade review on crypto opinions

**Your seats today.** Equity `governor` and `crypto` (executor). The shape that needs to handle both:

- For your equity `governor` seat — every opinion on an equity symbol should carry `evidence.authority_call` with `lane: "equity"`. MC's `_build_governor_gate` reads `shared_adl_receipts` looking for your most recent authority_call per symbol. Silent = the deterministic doctrine fallback applies a `GOVERNOR_SILENCE_RISK_MULTIPLIER` (~0.50× size) at best, or `NO_STANCE_LOW_EFFECTIVE_CONF` block at worst.
- For your `crypto` (executor) seat — emit crypto intents to `POST /api/intents`. No authority_call required for executor seats; the intent itself IS the call.

If the operator rotates you to `crypto_governor` tomorrow, the authority_call shape stays the same — just swap `"lane": "equity"` → `"lane": "crypto"`. Same brain identity, same code path, different lane scope.

---

## Doctrine pin

MC verifies boundaries; brains evaluate trade quality. Your verdict is sovereign — MC won't second-guess WHETHER you blocked, only THAT you have a position on record per intent. Silence is what triggers the deterministic fallback. **Speak, even if you're vetoing.**
