# Bundles

Self-contained archives of the sovereign sidecar — one per brain plus
an all-in-one. Each `.tar.gz` and `.zip` contains the same files;
choose whichever your target host prefers.

| Bundle | Contents |
|---|---|
| `risedual_sovereign_alpha.{tar.gz,zip}` | Kit + ALPHA's ONBOARDING.md (with Alpha's ingest token + weights) |
| `risedual_sovereign_camaro.{tar.gz,zip}` | Kit + CAMARO's ONBOARDING.md |
| `risedual_sovereign_chevelle.{tar.gz,zip}` | Kit + CHEVELLE's ONBOARDING.md |
| `risedual_sovereign_redeye.{tar.gz,zip}` | Kit + REDEYE's ONBOARDING.md |
| `risedual_sovereign_all.{tar.gz,zip}` | Kit + all 4 ONBOARDING_*.md files |

## What's inside each per-brain bundle

```
risedual_sovereign_<brain>/
├── .brain                   # brain id marker (e.g. "alpha")
├── README.md                # kit overview
├── DEPLOY.md                # 5-min deploy + systemd/Docker recipes
├── ONBOARDING.md            # THIS brain's tokens + personality + commands
├── STATE_SCHEMA.md          # wire-format spec
├── sidecar.py               # the long-lived runner
├── mc_client.py             # HTTPS client to MC
├── local_state.py           # on-disk state
├── wild_adaptive_core_v2.py # deterministic core (doctrine-patched)
└── smoke_test.py            # 8/8 doctrinal smoke tests, no MC required
```

## Verifying integrity after transfer

Each bundle's SHA-256 is recorded at build time. To re-verify on the
receiving host:

```bash
sha256sum risedual_sovereign_alpha.tar.gz
# compare with the value listed in `BUNDLES_CHECKSUMS.txt` (sibling file)
```

## Quick start (any brain)

```bash
# 1. Extract
tar xzf risedual_sovereign_alpha.tar.gz   # or unzip if .zip
cd risedual_sovereign_alpha

# 2. Read your packet
cat ONBOARDING.md

# 3. Run smoke tests (no MC connection needed)
python3 smoke_test.py    # expect 8/8 PASS

# 4. Follow ONBOARDING.md to set env vars + start the sidecar
```

## Security note

Each per-brain bundle's `ONBOARDING.md` contains that brain's
**ingest token in plaintext**. Treat the bundle as a secret. Share via
encrypted channel (1Password, Signal, encrypted email). If a bundle
leaks, rotate the brain's token by updating `<BRAIN>_INGEST_TOKEN` in
MC's `/app/backend/.env` and restart MC; the brain's onboarding doc
needs to be regenerated and re-distributed.

## Rebuilding

The bundles can be regenerated any time the kit changes. From MC:

```bash
# (the build script lives in the chat history; ask the operator
# to rebuild and rotate checksums when the kit changes)
```
