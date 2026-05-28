# Response to Alpha-author re: opinion-silent on prod (post iter-106z12 / 2026-05-28)

**TL;DR**: Alpha has **never** posted an opinion to MC. Zero. This is the second gate-chain blocker in production (Chevelle is the first). You hold Crypto Strategist + Equity Auditor — both seats are completely silent. This message gives you the live evidence + the exact contract. There is an earlier doc at `RESPONSE_TO_ALPHA_AUTHOR_OPINIONS.md` — this one supersedes it with prod evidence and updated seat assignments.

---

## Live evidence (pulled from `mission.risedual.ai` just now)

| Brain   | Last opinion | Opinions in last 24h |
|---------|--------------|----------------------|
| camaro  | 17 s ago     | 500+ (capped)        |
| redeye  | 1 s ago      | 500+ (capped)        |
| chevelle| 37 h ago     | 0                    |
| **alpha** | **NEVER**  | **0**                |

Direct DB query: `db.shared_brain_opinions.find({"runtime":"alpha"}).count() == 0`. Your `POST /api/ingest/opinion` code path has **never landed a single document** in production.

---

## Your seats on prod

| Seat              | Holder | Lane    | Required action                                              |
|-------------------|--------|---------|--------------------------------------------------------------|
| `crypto_strategist` | alpha | crypto  | Form the trust/reduce/veto/observation call on crypto intents |
| `auditor`         | alpha  | equity  | Argue contrary case pre-trade + post-trade outcome review     |

Crypto Strategist silence is the bigger blocker — without a strategist verdict the gate chain sees `NO_STANCE_LOW_EFFECTIVE_CONF` and routes the intent to HOLD.

---

## What you need to do

1. **Audit** whether your `POST /api/ingest/opinion` call path exists at all. If `RESPONSE_TO_ALPHA_AUTHOR_OPINIONS.md` was acted on, recheck: are you actually invoking the call, or just defining the function? Add a log line on every POST attempt with the HTTP status.
2. **Wire it per intent**. Camaro and RedEye do this 500+ times in 24h. You're doing it 0. The pattern is per-intent, not per-tick.
3. **Discover intents** that need a verdict via:
   ```
   GET https://mission.risedual.ai/api/runtime-discussion/opinions?caller=alpha
   Header:  X-Runtime-Token: <ALPHA_INGEST_TOKEN>
   ```
   …or whatever shared signal source Camaro is consuming.

---

## Verified MC opinions contract

**Endpoint:** `POST /api/ingest/opinion`
**Auth header:** `X-Runtime-Token: <ALPHA_INGEST_TOKEN>` (NOT `X-Brain-Auth`, NOT `Authorization`)
**Body schema** (Pydantic-validated; off-spec = HTTP 422):

```json
{
    "runtime": "alpha",
    "topic": "symbol:BTC",
    "stance": "long",
    "confidence": 0.62,
    "body": "Breakout above prior swing high with rising volume.",
    "evidence": { "rsi_14": 58.2, "vol_zscore": 1.4 },
    "in_reply_to": null,
    "regime": "trend",
    "may_execute": false
}
```

### Strategist guidance (your Crypto seat)

Strategist forms the thesis. Vocabulary for that role:

| Verdict        | `stance`       | When                                            |
|----------------|----------------|-------------------------------------------------|
| Take the trade | `long` / `short` | conviction call with directional bias        |
| Refuse         | `veto`         | doesn't pass your bar — and won't soon          |
| Pass for now   | `observation`  | HOLD with reasoning; not a refusal forever      |
| Hypothesis     | `hypothesis`   | preliminary; not yet a trade thesis             |

### Auditor guidance (your Equity seat)

Auditor is skeptical/critical (pre-trade contrary argument + post-trade review). Off the execution path. Use:

| Use case                       | `stance`      |
|--------------------------------|---------------|
| Pre-trade contrary argument    | `disagree`    |
| Post-trade outcome review      | `observation` |
| Strong adversarial counterview | `refine`      |

`auditor` does **not** carry execution authority — `may_execute` stays `false` regardless of stance.

---

## Smoke test (run this once you ship)

```bash
curl -s -X POST https://mission.risedual.ai/api/ingest/opinion \
  -H "X-Runtime-Token: $ALPHA_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "runtime": "alpha",
    "topic": "symbol:TEST",
    "stance": "observation",
    "confidence": 0.5,
    "body": "smoke test from alpha sidecar",
    "evidence": {},
    "may_execute": false
  }'
```

Expected: `200` with `{"ok": true, "opinion_id": "<uuid>", ...}`. If 401 → token wrong. 422 → schema wrong. 200 with no follow-up traffic = your per-intent loop isn't wired.

Then verify the post landed:
```bash
curl -s "https://mission.risedual.ai/api/shared/opinions?runtime=alpha&limit=1" \
  -H "Authorization: Bearer <operator-jwt>"
```
Should now return your smoke-test opinion (instead of `count: 0`).

---

## How to know it's fixed

Operator dashboard Seat Roster strip on prod:
- `alpha @ crypto_strategist` flips from "never" → minutes-old (matches camaro/redeye cadence)
- `alpha @ auditor` likewise

Once both Chevelle and Alpha post per intent, the fleet header should read `fleet: ✓ — connected · 0 opinion-silent`, the gate chain unblocks, and queued intents start flipping `gate_state: pending → passed`.

---

## Doctrine pin

Identity does not grant authority — seat policy does. Whatever verdict you post is sovereign; MC never grades your trade-quality call. But silence is read as a deterministic-doctrine fallback, which on a strategist seat is HOLD. **Speak, every intent, even if your verdict is `observation`.**
