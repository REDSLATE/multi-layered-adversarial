# Env variables checklist

## On Mission Control (already done if you're reading this from the repo)

```bash
# /app/backend/.env
RISEDUAL_PUBLIC_TOKEN="<the shared secret>"
```

The current dev value is in `/app/backend/.env`. Rotate before going to
production; share the new value with risedual.ai's backend env via
your secret manager.

## On risedual.ai's backend

```bash
# .env (or your secrets manager)
MC_BASE_URL="https://mc.risedual.io"                # MC's external URL
MC_PUBLIC_TOKEN="<same value as RISEDUAL_PUBLIC_TOKEN above>"
```

If you deploy MC at a different hostname, update `MC_BASE_URL`
accordingly. `mcPublicClient.ts` reads both at runtime — no rebuild
needed when the URL changes.

## On risedual.ai's frontend

**Nothing.** The frontend never sees the token. All MC calls go through
your backend's proxy routes. If you find yourself wanting to put
`MC_PUBLIC_TOKEN` in `NEXT_PUBLIC_*` or any client-bundled env, stop —
that defeats the trust model.

## Rotation procedure

1. On MC: generate a new value, add as `RISEDUAL_PUBLIC_TOKEN_NEW` env var, restart backend (now accepts both).
   - (Note: today MC only reads `RISEDUAL_PUBLIC_TOKEN`. For zero-downtime rotation, ask for a dual-token grace mode — Phase 2 enhancement. For now, rotate during a brief maintenance window.)
2. On risedual.ai backend: update `MC_PUBLIC_TOKEN` to the new value, restart / redeploy.
3. On MC: remove the old `RISEDUAL_PUBLIC_TOKEN` value (replace with the new), restart backend.

## Health check

```bash
curl -i https://mc.risedual.io/api/public/signals \
  -H "X-RiseDual-Token: $MC_PUBLIC_TOKEN" \
  -H "X-RiseDual-User-Tier: free"
```

Expected: `HTTP/1.1 200 OK` with JSON body. A 401 means the token
doesn't match. A 503 means MC's env var isn't set. A 422 means the
tier header is misspelled.
