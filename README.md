# RISEDUAL Mission Control

Operator console + execution gating + ladder doctrine for the RISE_AI multi-brain
trading system.

## Key documents in this repo

| File | Audience | Purpose |
|---|---|---|
| **[BRAIN_DEVELOPER_GUIDE.md](BRAIN_DEVELOPER_GUIDE.md)** | Brain pod teams (Alpha, Camaro, Chevelle, REDEYE) | **Single source of truth** for the brain → MC contract. POST shape, doctrine_snapshot, runtime stamp, observation receipts, ladder, prohibitions. |
| [RISE_AI_KERNEL.py](RISE_AI_KERNEL.py) | New engineers, stakeholders | High-level architecture in one file. The 7 boxes of RISE_AI. |
| `memory/PRD.md` | Operator, future agents | Original problem statement, dated changelog, prioritized backlog. |
| `memory/test_credentials.md` | Operator, testing agent | Admin credentials for `mission.risedual.ai`. |
| `backend/tests/README.md` | Engineers | Tripwire suite conventions. 269+ tripwires pin doctrine invariants. |

## Quick links

- Production: https://mission.risedual.ai
- Auth: `/admin@risedual.io` (creds in `memory/test_credentials.md`)
- Diagnostics: `/admin/diagnostics`
- Learning ladder: `GET /api/admin/learning-ladder`
- Observation receipts: `GET /api/admin/observation-receipts/counts`

## If you are a brain pod team integrating with MC

→ **Read `BRAIN_DEVELOPER_GUIDE.md`** first.

Do not read MC internals (`backend/shared/`, `backend/routes/`) before reading the guide.
Most "MC is doing something weird" reports turn out to be contract violations on the brain
side covered explicitly in the guide.

## If you are an engineer touching MC internals

→ Run `pytest -m tripwire -q` before AND after any change.

The tripwire suite is the codified doctrine. Breaking a tripwire means you broke an
intentional invariant. If you genuinely intend to change doctrine (e.g., adding a new gate
to the chain), the tripwire MUST be updated in the same commit and the change documented
in `memory/PRD.md` with a dated section.

## If you are the operator

→ `/admin/diagnostics` is the one-stop dashboard for everything.

Real-time brain liveness, sidecar identity verdict, lane execution toggles, live trade
probe, runtime tokens, doctrine health. Open the sidecar identity panel first when
investigating "why isn't a brain trading".
