"""Shared doctrine package.

Two surfaces:
    1. `shared.doctrine.routes.router` — Markdown doctrine packet server
       (`GET /api/doctrine/{name}` etc.). Re-exported as `router` here
       so the legacy `from shared.doctrine import router` in server.py
       still works after the package conversion.
    2. `shared.doctrine.base_labels` — lane-neutral setup-quality labeler.
       Consumed by `runtimes/{brain}/doctrine_interpreter.py` modules to
       produce role-flavored sidecar packets bundled via
       `shared.doctrine.brain_sidecars.build_all_brain_doctrine_packets`.
"""
from shared.doctrine.routes import router  # noqa: F401 — re-export
from shared.doctrine.scorecard import router as scorecard_router  # noqa: F401
