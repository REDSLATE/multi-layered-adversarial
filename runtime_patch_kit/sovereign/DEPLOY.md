# Sovereign Sidecar — Deploy Runbook

> Use this when you're ready to put one of the 4 brains (Alpha / Camaro
> / Chevelle / REDEYE) on a host so it starts talking to Mission Control.
>
> No prerequisites beyond Python 3.11+ and outbound HTTPS to MC.

## What you're deploying

A **single Python process** (`sidecar.py`) that:
- Loads or creates a JSON state file on the host (`~/.risedual/<brain>/state.json`).
- Every 60 seconds, runs the deterministic core on whatever symbols you configured.
- POSTs sovereign-state snapshots + heartbeat pings to MC.
- Never trades, never writes to MC's DB directly. Three observation-only locks in place.

## What it requires on the host

| | |
|---|---|
| OS | Anything with Python 3.11+ (Linux, macOS, Windows + WSL). |
| Disk | < 50 MB; the state file caps at ~10 MB for 5000 decisions. |
| Network | Outbound HTTPS to your MC base URL. No inbound ports needed. |
| Auth | One ingest token per brain (Mission Control issues these). |

## 5-minute deploy (any host, any brain)

```bash
# 1. Clone or copy the sovereign kit to the host.
#    Pick ONE of these:
#      (a) git clone <your-mc-repo>; cd <repo>/runtime_patch_kit/sovereign
#      (b) scp -r /local/path/to/runtime_patch_kit/sovereign host:~/sovereign
#          ssh host && cd ~/sovereign

# 2. Install dependencies (none required — stdlib only).
python3 -V    # confirm 3.11+

# 3. Set env vars (use the per-brain packet for the exact values).
export MC_BASE_URL="https://multi-brain-backbone.preview.emergentagent.com"
export ALPHA_INGEST_TOKEN="alpha-ingest-2cf91b5e-3a44-4c1b-9e07-4e1b7d2c3a55"
# (or CAMARO_INGEST_TOKEN / CHEVELLE_INGEST_TOKEN / REDEYE_INGEST_TOKEN)

# 4. Run the smoke test (no MC connection required — verifies the doctrine locks).
python3 smoke_test.py     # expected: 8/8 PASS

# 5. Start the sidecar.
python3 sidecar.py --brain alpha --mode DTD --symbols BTC/USD ETH/USD --interval 60
```

Within ~60 seconds you should see in MC:
- The brain's row appear in `GET /api/admin/sovereign/state`.
- Heartbeat pings landing at `GET /api/heartbeat-ping/{brain}`.
- The Sovereign State tile populated on `/runtime/{brain}`.

## Running as a long-lived process

For dev / testing, just run in a screen or tmux. For production:

### systemd (Linux)

`/etc/systemd/system/risedual-sovereign-<brain>.service`:

```ini
[Unit]
Description=RISEDUAL Sovereign Sidecar — <brain>
After=network-online.target

[Service]
Type=simple
User=risedual
WorkingDirectory=/opt/risedual/sovereign
Environment="MC_BASE_URL=https://multi-brain-backbone.preview.emergentagent.com"
Environment="ALPHA_INGEST_TOKEN=<token-from-per-brain-packet>"
ExecStart=/usr/bin/python3 sidecar.py --brain alpha --mode DTD --symbols BTC/USD ETH/USD --interval 60
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now risedual-sovereign-alpha
sudo journalctl -fu risedual-sovereign-alpha
```

### Docker

```Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY runtime_patch_kit/sovereign/ ./
CMD ["python3", "sidecar.py", "--brain", "alpha", "--mode", "DTD", "--symbols", "BTC/USD"]
```

```bash
docker build -t risedual-sovereign:alpha .
docker run -d \
  --name risedual-sovereign-alpha \
  --restart unless-stopped \
  -e MC_BASE_URL=https://multi-brain-backbone.preview.emergentagent.com \
  -e ALPHA_INGEST_TOKEN=<token-from-per-brain-packet> \
  -v /var/risedual/alpha:/root/.risedual/alpha \
  risedual-sovereign:alpha
```

## Verifying from MC's side

In your operator browser, hit `/runtime/<brain>` — the **Sovereign State** tile should populate within a minute.

From the CLI:
```bash
# Replace TOKEN with your operator JWT.
curl -s "$MC_BASE_URL/api/admin/sovereign/state" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Each connected brain shows up with `last_contribution_at`, current weights, mode, and seat snapshot.

## Switching modes (DTD ↔ PRD)

- **DTD** (Deterministic Training Data): brain is replaying historical / labeled bars. Weight updates ARE allowed. MC accepts `training_signal=true`.
- **PRD** (Production): brain is reading live market data. MC REJECTS any `training_signal=true` payload with 422 — to learn, the brain must restart in DTD mode and replay against historical bars.

Switching is a process restart with a different `--mode` flag. The on-disk state file is preserved; only the runtime mode flag changes.

## Connecting to a real broker feed

The kit ships with a **synthetic top-of-book stub** so the sidecar can dry-run on any host without broker connectivity. To wire real bars:

1. Subclass `SovereignSidecar` on YOUR brain host (don't modify the kit).
2. Override `top_of_book_fn` with your broker poller (Kraken WebSocket, TOS bars, Public.com REST, etc.).
3. See the README in this folder for a Kraken example.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `RuntimeError: DOCTRINE VIOLATION` at startup | Someone flipped `LIVE_TRADING_ENABLED=True` in the local copy of `wild_adaptive_core_v2.py`. Fix it back to False — the sidecar refuses to start otherwise. |
| `MCClientError: ... → 401` on every POST | Wrong token. Confirm the env var matches MC's `<BRAIN>_INGEST_TOKEN`. |
| `MCClientError: ... → 422` with `live_trading_enabled` mention | Same as above — local file was tampered. Reset. |
| `MCClientError: ... → 422` with `PRD` mention | Brain is in PRD mode but sent `training_signal=true`. Either flip to DTD or remove the training flag from your custom code. |
| No row in MC's `/api/admin/sovereign/state` | Sidecar isn't reaching MC. Check `MC_BASE_URL` resolves; test `curl -i "$MC_BASE_URL/api/health"` from the host. |
| Heartbeat shows stale > 5 min on MC dashboard | Sidecar crashed. Check journalctl / docker logs. The runner loop swallows tick failures; only a Python crash kills the process. |

## Updating the kit

The kit is forward-compatible. When MC adds new endpoints, the sidecar keeps working — it only calls a small surface. To update:

```bash
cd /opt/risedual/sovereign
git pull   # or scp the new files
sudo systemctl restart risedual-sovereign-<brain>
```

State file format is versioned (`schema_version` in `local_state.py`); the loader is forward-compatible across format changes.

## What's NOT in this kit (intentionally)

- **Broker secrets.** Wire those on the host, outside the kit.
- **MC's mongo URL or any admin auth.** Brains talk to MC via the public-facing API only.
- **Trade execution.** Phase 1 is observation-only. The kit will refuse to start if its local copy of the core has `LIVE_TRADING_ENABLED=True`.

## Where to go after deploying one brain

1. Repeat for the other 3 brains. Use different `--symbols` and `--mode` to give each one a distinct personality.
2. Watch `/runtime/{brain}` on MC during the first 24h — confirm weights are drifting (DTD mode) and the seat snapshot is captured.
3. When ready, hand the same paste-message + values to risedual.ai's agent so the public site starts pulling signals from MC.
