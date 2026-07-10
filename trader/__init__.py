"""RISEDUAL Trader — MC support library (2026-02-19 excision).

**Historical note**: this package used to run a standalone sidecar
loop (Path 2) that competed with Mission Control's `auto_router` for
broker authority. That sidecar was demoted to shadow mode in
iter-22 and formally deleted in iter-23. The One Broker Door doctrine
now lives entirely inside MC's `shared/auto_router.py`.

What remains here is the still-useful support layer that MC's admin
dashboard and Webull login flow depend on:

    * `webull_auth`   — OAuth token cache + refresh (used by
                        `routes/webull_credentials.py`)
    * `spread`        — spread poller (dashboard-only tile; started
                        by MC lifespan when the stream is disabled)
    * `spread_stream` — Webull v2 live-quote stream (dashboard)
    * `store`         — SQLite truth-tape + JSONL receipts (read by
                        `/api/admin/trader/*` endpoints)
    * `state`         — hydrated in-memory state (dashboard reads)
    * `merge_rights`  — CFQS computation (admin endpoint)
    * `config`        — env-var accessors used by the above modules

No orchestration lives here. No `main.py`. No broker adapter. No
sidecar risk gate. If you find yourself importing from this package
inside a code path that hits a broker, STOP — that call belongs in
`shared/auto_router.py` or one of the `shared/broker/*` adapters.
"""
