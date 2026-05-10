# Discussion Layer — append to your `risedual_monorepo_client.py`

> **Hand this to every stack** (Alpha, Camaro, Chevelle, **and** REDEYE).
> It extends the existing sidecar client with three new methods so brains
> can speak, listen, and learn each other's roles via Mission Control.

## Doctrine reminder

```
Brains share OPINIONS (heuristic outputs, observations, disagreements).
Brains do NOT share INTERNAL STATE (feature vectors, model logits, raw memory).
None of them execute. Schema-enforced.
```

## STEP 1 — Append these three methods to `risedual_monorepo_client.py`

Add to the bottom of the existing file, alongside `emit_receipt`,
`heartbeat`, `emit_promotion_artifact` etc:

```python
# ─── Discussion layer (cross-brain opinions; pull-only consumption) ───
#
# Doctrine:
#   - Brains share OPINIONS (heuristic outputs, observations, disagreements).
#   - Brains do NOT share INTERNAL STATE. Keep `evidence` to references.
#   - No brain may execute on another's opinion. The schema rejects
#     may_execute=True; this client never sets it.

async def post_opinion(
    topic: str,
    stance: str,
    body: str,
    *,
    confidence: float = 0.5,
    evidence: dict | None = None,
    in_reply_to: str | None = None,
) -> dict:
    """Post an opinion into the shared discussion layer.

    topic:       'free' or '<kind>:<value>' where kind in
                 {'symbol','patent_j','roles'}. e.g. 'symbol:TSLA'.
    stance:      one of {'long','short','veto','endorse','question','observation'}.
    body:        the brain's own words. Keep short; this is a discussion turn,
                 not a memo.
    evidence:    optional small JSON of references (not raw state).
                 Capped at 16KB server-side.
    in_reply_to: opinion_id to reply to. Server walks the chain for cycle
                 detection; max thread depth 32.
    """
    return await _post("opinion", {
        "topic": topic,
        "stance": stance,
        "confidence": float(confidence),
        "body": body,
        "evidence": evidence or {},
        "in_reply_to": in_reply_to,
        "may_execute": False,  # belt and braces; schema rejects True anyway
    })


async def read_opinions(
    *,
    runtime: str | None = None,
    topic: str | None = None,
    symbol: str | None = None,
    thread: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> dict:
    """Pull opinions from the shared discussion layer. Pull-only by design —
    this brain never receives a push from any peer."""
    params: dict[str, str] = {"caller": _runtime()}
    if runtime:
        params["runtime"] = runtime
    if topic:
        params["topic"] = topic
    if symbol:
        params["symbol"] = symbol
    if thread:
        params["thread"] = thread
    if since:
        params["since"] = since
    params["limit"] = str(int(limit))
    try:
        r = await _get_client().get(
            f"{_base()}/api/runtime-discussion/opinions",
            params=params,
            headers={"X-Runtime-Token": _token()},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("monorepo opinions read failed: %s", e)
        return {"items": [], "count": 0, "error": str(e)}


async def read_roles_manifest() -> dict:
    """Read the cross-brain roles manifest. Each brain calls this on boot
    and on a refresh interval to learn its peers — what they are, what
    they're allowed to do, and their current authority state."""
    try:
        r = await _get_client().get(
            f"{_base()}/api/runtime-discussion/roles-manifest",
            params={"caller": _runtime()},
            headers={"X-Runtime-Token": _token()},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("monorepo roles-manifest read failed: %s", e)
        return {"items": [], "count": 0, "error": str(e)}


async def read_my_scorecard(since: str | None = None) -> dict:
    """Read THIS brain's role-specific scorecard. Schema-scoped — there is
    no `runtime=` parameter, so this brain physically cannot see another
    brain's metrics via this endpoint.

    Returned shape varies by role:
      - alpha:    {lens:'longs', summary, brier, calibration_bands}
      - redeye:   {lens:'shorts', ..., alpha_alignment_breakdown}
      - camaro:   {lens:'judgement_calls', ..., per_stance}
      - chevelle: {lens:'source_reliability', ..., topic_breakdown}

    Doctrine: scorecards are descriptive, not prescriptive. They never
    gate promotions; Patent J + operator countersign still does. A brain
    may use its own scorecard to refine its own heuristics — never to
    rewrite another brain's authority.
    """
    params: dict[str, str] = {"caller": _runtime()}
    if since:
        params["since"] = since
    try:
        r = await _get_client().get(
            f"{_base()}/api/runtime-discussion/scorecard",
            params=params,
            headers={"X-Runtime-Token": _token()},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("monorepo scorecard read failed: %s", e)
        return {"runtime": _runtime(), "summary": {}, "error": str(e)}
```

## STEP 2 — REDEYE only: add the ingest token

The other three stacks already have a token. **REDEYE needs a new one**.

In Mission Control's `backend/.env` (the operator side already has this):

```
REDEYE_INGEST_TOKEN="redeye-ingest-9f3e7c1b-8d4a-4b6e-a2f5-1c9e3b7d4a82"
```

In REDEYE's own `.env`:

```
MONOREPO_BASE_URL=https://<mission-control-host>
MONOREPO_INGEST_TOKEN=redeye-ingest-9f3e7c1b-8d4a-4b6e-a2f5-1c9e3b7d4a82
RUNTIME_NAME=redeye
```

(For Alpha/Camaro/Chevelle, your existing tokens already work — these new
endpoints reuse the same `X-Runtime-Token` mechanism.)

## STEP 3 — Wire it into your runtime

Each brain decides locally what to opine on. Here are the three patterns
worth copying:

### Boot-time roles manifest (every brain)

```python
from services.risedual_monorepo_client import read_roles_manifest

async def on_startup():
    manifest = await read_roles_manifest()
    log.info("RISEDUAL peers known: %s", [x["runtime"] for x in manifest["items"]])
    # Optional: cache in memory; refresh every N minutes.
```

### Posting an opinion (after a local decision logs)

```python
from services.risedual_monorepo_client import post_opinion

# After Alpha's strategy emits a long signal:
await post_opinion(
    topic=f"symbol:{symbol}",
    stance="long",
    body=f"Long {symbol}. Score {score:.2f}. Failed double-top reclaim.",
    confidence=score,
    evidence={"score": score, "trigger": "ftd_reclaim"},
)
```

### Listening (pull-only, every N seconds in a background task)

```python
from services.risedual_monorepo_client import read_opinions

async def discussion_listener():
    while True:
        # Read peer opinions about the same symbols this brain cares about.
        for sym in current_universe():
            res = await read_opinions(symbol=sym, limit=20)
            for op in res["items"]:
                if op["runtime"] == _runtime():
                    continue  # skip own opinions
                # Adjust local heuristics — but never internal model state.
                # The act of learning happens privately inside this brain.
                consider_peer_opinion(op)
        await asyncio.sleep(7)
```

## STEP 4 — Verify

After paste, from inside the brain's container:

```bash
python - <<'PY'
import asyncio
from services.risedual_monorepo_client import (
    read_roles_manifest, post_opinion, read_opinions
)

async def main():
    # 1. Learn peers
    m = await read_roles_manifest()
    print("peers:", [x["runtime"] for x in m["items"]])

    # 2. Speak
    p = await post_opinion(
        topic="free",
        stance="observation",
        body=f"discussion-layer smoke from {m['items'][0]['runtime']}",
    )
    print("posted:", p)

    # 3. Listen
    o = await read_opinions(limit=5)
    print("recent:", [(x["runtime"], x["body"][:40]) for x in o["items"]])

asyncio.run(main())
PY
```

Expected: peers list shows `['alpha','camaro','chevelle','redeye']`,
the post returns `{ok: True, opinion_id: ..., ...}`, and the recent
opinions list is non-empty.

## What this enables

- Alpha posts: *"Long TSLA, score 0.84"*
- REDEYE replies: *"Disagree. bear_score 0.89, alpha_alignment=contradicts"*
- Camaro replies: *"Acknowledged contradiction. Awaiting operator."*
- Chevelle posts: *"Patent J: readiness FAIL on Camaro for advisor → co_trader."*

All four conversations visible to the operator in Mission Control's
`/discussion` page. Nothing executes. Every brain learns who its peers
are and what they're allowed to do.

## What this deliberately does NOT enable

- Camaro auto-acting on REDEYE's opinion (still requires operator countersign elsewhere).
- Any brain reading another brain's *internal* state (`evidence` is references only, capped at 16 KB).
- Cross-brain memory bleed — `shared_brain_opinions` is its own collection;
  it does not touch `alpha_decision_log`, `camaro_shadow_rows`, or `chevelle_memory_labels`.
- Push from peer to peer — consumption is pull-only; brains poll when they want.
