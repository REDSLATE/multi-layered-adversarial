# RISEDUAL Brain Runtime — Sidecar Hardening Note

**From:** Mission Control
**Date:** May 14, 2026
**Applies to:** Alpha, Chevelle, REDEYE
**Status of Camaro:** patched, running clean.

## What we found

Your sidecar is freezing silently. Three compounding bugs (Camaro found and
fixed all three earlier today):

1. **`httpx.Client(timeout=5.0)`** uses a long-lived keep-alive connection.
   When MC's load balancer rotates a backend pod, the cached socket goes
   half-open. Next POST blocks indefinitely in TLS handshake. The
   single-number `timeout=5.0` doesn't reliably trip on a `pool` acquire
   stall.
2. **Heartbeat + contribution + state save + decision compute share one
   `tick()` body.** Any one hang freezes everything.
3. **No watchdog.** Supervisor sees the process as `RUNNING` because it
   technically is — just blocked in a syscall. `autorestart=true` never
   fires.

## Fix #1 — replace the httpx Client init

In your `mc_client.py` (or equivalent — the file where you `import httpx` and
create the MC client), replace:

```python
self._client = httpx.Client(timeout=timeout)
```

with:

```python
self._client = httpx.Client(
    timeout=httpx.Timeout(connect=3.0, read=timeout, write=5.0, pool=2.0),
    limits=httpx.Limits(max_keepalive_connections=0, max_connections=4),
)
```

This bounds every phase individually AND disables keep-alive entirely.
Sidecar makes ~1 req/min so the cost of a fresh TLS handshake each tick
is negligible compared to a silent freeze.

> **Chevelle note:** This engine's `mc_client.py` uses stdlib `urllib`, not
> `httpx` (per the kit's no-deps doctrine). urllib's `urlopen` already
> opens a fresh connection per call (no keep-alive pool), so the
> rotated-pod failure mode is mitigated by default. We additionally
> tightened the default timeout to 5s, set a process-wide
> `socket.setdefaulttimeout(5.0)` (covers DNS), and wrapped each MC POST
> in a `threading`-based per-call timeout so a syscall stall doesn't
> hang the tick.

## Fix #2 — split the loops

Right now you probably have something like:

```python
async def tick():
    await heartbeat()
    await post_contribution()
    await save_state()
    await compute_decision()
    await maybe_post_intent()
```

Split into independent asyncio tasks so a hang in one doesn't kill the
others:

```python
asyncio.create_task(_heartbeat_loop())     # dumbest possible, just pings MC
asyncio.create_task(_contribution_loop())
asyncio.create_task(_decision_loop())
```

Heartbeat should be the boring one — it never touches compute, never blocks.
If contribution or decision hangs, MC still sees "alive but quiet" → operator
can act before everything looks dead.

> **Chevelle note:** This sidecar is sync (not asyncio). Equivalent applied:
> a dedicated background `threading.Thread` (daemon) runs the heartbeat
> loop independent of `_run_tick`. If decide/contribution/opinion hangs
> for any reason, heartbeat keeps flowing and MC sees "alive but quiet."

## Fix #3 — add the liveness watchdog

In your main tick body, after a successful run:

```python
from pathlib import Path
LIVENESS_FILE = Path("/tmp/{brain}_alive")  # replace {brain}
LIVENESS_FILE.touch()
```

Then add this to your supervisor config (or a cron):

```bash
*/1 * * * * [ $(($(date +%s) - $(stat -c %Y /tmp/{brain}_alive 2>/dev/null || echo 0))) -lt 120 ] || pkill -9 -f {brain}_sidecar
```

(Replace `{brain}` with `alpha`, `chevelle`, or `redeye`.)

Hard-kills the process after 2 minutes of liveness silence → supervisor
restarts → freeze auto-recovers.

> **Chevelle note:** Liveness file at `/tmp/chevelle_alive` is touched
> after each successful local decide+state-save phase (before any MC POST
> attempt — so a hang in MC posts surfaces as stale liveness). A second
> supervisor program (`sovereign-chevelle-watchdog`) runs the kill check
> every 30s.

## Verification after deploy

From your runtime machine:
```bash
curl https://mission.risedual.ai/api/heartbeat-status/{brain}
```
`age_seconds` should stay below 60 indefinitely. If you see it climb past
90 again, something is still freezing.

## How MC will confirm you're healthy

We watch your `heartbeat_age_seconds` and `contribution_age_seconds`
divergence. If heartbeat is fresh but contribution is stale, fix #2
worked but you still have a hang in the decision/contribution loop —
we'll surface that for triage.

— MC
