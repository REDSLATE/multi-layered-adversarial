# RISEDUAL Brain Runtime — Config Note

**From:** Mission Control (`mission.risedual.ai`)
**Date:** May 14, 2026
**Issue:** You are pointed at the wrong Mission Control URL. Please update and restart.

## What to change

Find your `.env` file (or your runtime's environment-variable config). Look for any one of these keys:

```
MC_URL
MISSION_CONTROL_URL
MC_BASE_URL
RISEDUAL_MC_URL
```

It is currently set to something like:
```
https://multi-brain-backbone.preview.emergentagent.com
```
(or another URL ending in `.preview.emergentagent.com`)

**Change it to exactly this:**
```
https://mission.risedual.ai
```

No trailing slash. No `/api`. Just the hostname above.

## Do NOT change

- `X-Runtime-Token` / `INGEST_TOKEN` — keep your existing token. Mission Control still recognizes the same token on the new URL.
- Your heartbeat interval, intent posting cadence, or any other behavior.

## After updating

Restart your runtime process. Within 30–60 seconds you should:

1. See `200 OK` from `POST /api/ingest/heartbeat`
2. See `200 OK` from `POST /api/intents` (when you next emit one)

## How Mission Control will confirm you're back

We watch:
```
GET https://mission.risedual.ai/api/admin/diagnostics
```
Your runtime's `heartbeat_age_seconds` should drop below 60. When all four engines are fresh, the auto-router will begin executing Camaro's queued paper intents on Alpaca.

## If your runtime breaks after the change

- Make sure the URL is exactly `https://mission.risedual.ai` (https, no path, no slash)
- Confirm your `*_INGEST_TOKEN` is still set
- Try `curl https://mission.risedual.ai/api/health` from your runtime's machine — you should get `{"ok":true,"mongo":true,...}`

If any of those fail, send a screenshot back to Mission Control and we'll trace it.

— MC

---

## One-line smoke test

Substitute `<BRAIN>` with `alpha`, `camaro`, `chevelle`, or `redeye`, and `$TOKEN` with your runtime's ingest token, then paste:

```bash
MC=https://mission.risedual.ai; BRAIN=<BRAIN>; \
echo "== /api/health ==" && curl -s -o - -w "\nHTTP %{http_code}\n" "$MC/api/health" && \
echo "== /api/ingest/heartbeat ==" && curl -s -o - -w "\nHTTP %{http_code}\n" \
  -X POST "$MC/api/ingest/heartbeat" \
  -H "Content-Type: application/json" \
  -H "X-Runtime-Token: $TOKEN" \
  -d "{\"runtime\":\"$BRAIN\",\"status\":\"ok\"}"
```

Expected: two `HTTP 200` blocks. `/api/health` returns `{"ok":true,"mongo":true,...}`. `/api/ingest/heartbeat` acks the heartbeat.

If either returns 4xx/5xx, ping MC with the response body.
