# Response to Alpha-author re: opinion-silent diagnosis

**TL;DR**: Your diagnosis is correct — Alpha posts sovereign-audit but never posts opinions. The fix direction is right: mirror Camaro/Chevelle's POST pattern. However, your proposed POST body and URL don't match MC's actual contract. Verified spec below.

---

## Verified MC opinions contract

**Endpoint:** `POST /api/ingest/opinion` (NOT `/api/opinions`)

**Auth header:** `X-Runtime-Token: <ALPHA_INGEST_TOKEN>` (NOT `X-Brain-Auth`)

**Body schema** (Pydantic-validated server-side; rejects anything off-spec):

```python
{
    "runtime": "alpha",                        # literal: alpha|camaro|chevelle|redeye
    "topic": "symbol:AAPL",                    # "free" OR "<kind>:<value>"
    "stance": "long",                          # closed vocabulary (see below)
    "confidence": 0.5,                         # 0.0..1.0
    "body": "Brief reasoning for the verdict", # 1..~2000 chars
    "evidence": {},                            # optional, JSON-serializable
    "in_reply_to": None,                       # optional opinion_id for threading
    "regime": "trend",                         # optional regime tag
    "may_execute": False                       # MUST be False — schema-rejected otherwise
}
```

**Stance vocabulary** (precise — not synonyms):
- `long` / `short` — independent directional verdict
- `endorse` — agree-with-prior (referenced via `in_reply_to`)
- `veto` — block authority
- `observation` — neutral commentary / HOLD with reasoning
- `question`, `agree`, `disagree`, `refine`, `retract`, `hypothesis`

**Lands in:** `shared_brain_opinions` collection (NOT `shared_intents`)

**Schema rejects (hard 400/422):**
- `may_execute != false` → "opinions never carry execution authority"
- `topic` not in `"free"` or `"<kind>:<value>"` format
- `runtime` not one of 4 known brains
- `stance` outside closed vocabulary
- empty `body`
- `evidence` exceeds MAX_EVIDENCE_BYTES

## Where to mirror the pattern in Alpha's sidecar

Camaro and Chevelle's sidecars already POST here. Look for the helper that
constructs the `OpinionIn` body and POSTs with
`X-Runtime-Token: os.environ["<BRAIN>_INGEST_TOKEN"]`. Drop the equivalent
in Alpha v1.6 alongside the existing sovereign-audit emit.

No code change needed on MC. The endpoint has been live since the early
monorepo days; Alpha just isn't calling it.

## Verdict mapping nuance

For BUY/SELL/HOLD verdicts you're probably emitting today:
- BUY → `stance: "long"`
- SELL → `stance: "short"`
- HOLD with reasoning → `stance: "observation"`
- "agree with Camaro's call" → `stance: "endorse"` + `in_reply_to: <Camaro's opinion_id>`

## Patch template for the OTHER opinion-silent brains

Once Alpha's fix lands, the same patch with `runtime="redeye"` can ship to
the RedEye sidecar — the diagnosis turns into a template, not a one-off.

## Acknowledged: this is on production

Alpha sidecar lives on a separate deployment from MC. The patch + redeploy
is on the brain team's side. MC's role here is just publishing the verified
contract — which this doc does.
