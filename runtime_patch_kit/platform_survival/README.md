# Platform Survival Layer — Portable Drop-In

## What this is

A self-contained module that gives every RISEDUAL brain stack
(Alpha · Camaro · Chevelle · REDEYE) **the same execution doctrine**,
regardless of which hosting platform it runs on (Emergent, Railway,
Render, VPS, local).

The doctrine:

> Sidecars communicate. MC approves. RoadGuard protects.
> Broker executes only with MC receipt.
> Preview is never proof of PROD.

The layer makes that doctrine **executable code, not policy text**:

1. **`RuntimeStamp.current()`** — every sidecar boot stamps its identity
   (env, git_sha, platform, MC URL, DB name, broker mode, policy hash)
   and explicitly carries `local_execution_authority=False`.
2. **`sidecar_build_intent(...)`** — the only way a sidecar produces an
   intent envelope. Sidecars can't approve execution.
3. **`mc_canonical_gate(intent)`** — Mission Control's single gate. It
   inspects the stamp, runs lane / confidence checks, and emits an
   HMAC-signed `MCExecutionReceipt`.
4. **`broker_verify_receipt(receipt)`** — broker adapter (Alpaca,
   Kraken, IBKR) refuses any order whose receipt isn't signed by the
   live `RISEDUAL_MC_RECEIPT_SECRET`.

If the policy ever changes shape, `policy_hash()` changes — sidecars
running stale policy are rejected by the canonical gate with a typed
`POLICY_HASH_MISMATCH` error. You never have to wonder again whether a
preview deploy snuck into PROD.

## Why placement, not patch

Brain sidecars (Alpha / Camaro / Chevelle / REDEYE) live in their own
repos and run on their own hosting. They cannot **rent** doctrine from
Mission Control — when MC is down or migrating between platforms, the
sidecars still need to know:

- They are not the authority.
- Their PROD identity is verifiable.
- An old policy hash means they refuse to act.
- An MC-signed receipt is the only thing that approves a fill.

So this package gets **copied into each stack's repo** rather than
imported across the network.

## Drop-in

```
cp -r services/platform_survival  <YOUR_STACK_REPO>/backend/services/platform_survival
cp tests/test_platform_survival.py             <YOUR_STACK_REPO>/backend/tests/
cp tests/test_no_duplicate_execution_gates.py  <YOUR_STACK_REPO>/backend/tests/
```

No new dependencies. The module uses only the standard library
(`hashlib`, `hmac`, `json`, `os`, `time`, `dataclasses`).

## Run tests inside the stack

```
cd <YOUR_STACK_REPO>/backend
pytest tests/test_platform_survival.py tests/test_no_duplicate_execution_gates.py -q
```

The first three tests prove the doctrine math. The fourth proves a
tampered receipt is rejected. The CI tripwire prevents anyone from
re-introducing old per-platform gate logic.

## Required env vars

| Variable | Set on | Purpose |
| --- | --- | --- |
| `RISEDUAL_ENV` | sidecar + MC | `prod` / `preview` / `local` — visible in every stamp |
| `RISEDUAL_PLATFORM` | sidecar + MC | `emergent` / `railway` / `render` / `vps` / `local` |
| `RISEDUAL_MC_URL` | sidecar + MC | canonical MC URL — `https://mission.risedual.ai` in PROD |
| `RISEDUAL_DB_NAME` | sidecar + MC | DB the stack reads/writes to |
| `RISEDUAL_BROKER_MODE` | sidecar + MC | `paper` / `live` / `dry_run` |
| `RISEDUAL_SIDECAR_VERSION` | sidecar | semver of the sidecar build |
| `GIT_SHA` | sidecar + MC | commit hash baked at build time |
| `RISEDUAL_MC_RECEIPT_SECRET` | **MC + broker adapters only** | HMAC key — never set on a sidecar |
| `RISEDUAL_CRYPTO_CONFIDENCE_FLOOR` | MC | minimum confidence to approve a crypto intent (default 0.45) |
| `RISEDUAL_EQUITY_CONFIDENCE_FLOOR` | MC | minimum confidence to approve an equity intent (default 0.45) |

**Security pin:** `RISEDUAL_MC_RECEIPT_SECRET` lives on MC and the broker
adapter only. If a sidecar can see it, the survival layer's signature
guarantee is broken.

## Adoption checklist per stack

See the four `PASTE_INTO_*_AGENT.md` files in this folder.

## Doctrine pin

This package will never grow new approval paths. New gate logic goes
inside `mc_canonical_gate(...)` only. Anything else is a violation
caught by `test_no_duplicate_execution_gates.py`.
