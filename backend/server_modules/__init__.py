"""Server-side modular components.

Extracted from the original monolithic `server.py` (2026-06-18 refactor)
to keep the entry point thin and the boot/worker/route registration
discoverable. Each module here owns one cross-cutting concern:

    lifespan.py          — FastAPI lifespan: boot migrations, worker
                           start/stop, graceful shutdown.
    router_registry.py   — All `api_router.include_router(...)` calls.
    middleware_setup.py  — CORS + public-API rate-limit/traffic
                           middleware wiring.
    meta_routes.py       — `/`, `/health`, `/admin/neutral-brains/status`
                           endpoints (previously inline in server.py).

Behavior is 1:1 with the pre-refactor server.py; no semantics changed.
"""
