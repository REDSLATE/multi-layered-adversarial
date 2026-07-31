"""Tripwires for the manifest-based router registry (2026-07-31).

The registry was refactored from ~300 explicit import/include lines
to an ordered `ROUTER_SPECS` manifest + `routes/` discovery sweep.
These tests pin the two things that can silently break:

1. ROUTE-TABLE EQUIVALENCE (incl. ORDER — FastAPI is first-match-
   wins on overlapping paths): the registered table must exactly
   match the snapshot captured from the pre-refactor registry.
   Regenerate after INTENTIONALLY adding/moving routes:

       cd /app/backend && python -c "
       import json
       from dotenv import load_dotenv; load_dotenv('.env')
       from fastapi import APIRouter
       from server_modules.router_registry import register_routers
       api = APIRouter(prefix='/api'); register_routers(api)
       t = [{'path': r.path, 'methods': sorted(r.methods or []), 'name': r.name} for r in api.routes]
       json.dump(t, open('tests/fixtures/route_table_snapshot.json','w'), indent=0)"

2. NO UNLISTED MODULES: every `routes/` module exposing `router`
   must be pinned in ROUTER_SPECS (or parked in SKIP_DISCOVERY) so
   the discovery WARNING path stays a prod-only safety net.
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.tripwire

FIXTURE = "/app/backend/tests/fixtures/route_table_snapshot.json"


def _build_table():
    from fastapi import APIRouter
    from server_modules.router_registry import register_routers
    api = APIRouter(prefix="/api")
    register_routers(api)
    return [
        {"path": r.path, "methods": sorted(r.methods or []), "name": r.name}
        for r in api.routes
    ]


def test_route_table_matches_snapshot_in_order():
    snapshot = json.load(open(FIXTURE))
    table = _build_table()
    assert len(table) == len(snapshot), (
        f"route count drifted: {len(table)} vs snapshot {len(snapshot)} — "
        "if intentional, regenerate the fixture (see module docstring)"
    )
    for i, (got, want) in enumerate(zip(table, snapshot)):
        assert got == want, (
            f"route #{i} drifted (order matters — first-match-wins): "
            f"got {got} want {want}"
        )


def test_router_specs_all_resolve_uniquely():
    from fastapi import APIRouter
    from server_modules.router_registry import ROUTER_SPECS, _resolve
    assert len(set(ROUTER_SPECS)) == len(ROUTER_SPECS), "duplicate specs"
    for spec in ROUTER_SPECS:
        obj = _resolve(spec)
        routers = obj if isinstance(obj, (list, tuple)) else (obj,)
        assert routers, spec
        for r in routers:
            assert isinstance(r, APIRouter), spec


def test_no_unlisted_route_modules():
    from fastapi import APIRouter
    from server_modules.router_registry import ROUTER_SPECS, SKIP_DISCOVERY
    listed = {s.split(":", 1)[0] for s in ROUTER_SPECS}
    import routes as routes_pkg
    unlisted = []
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        mod_path = f"routes.{info.name}"
        if mod_path in listed or info.name in SKIP_DISCOVERY:
            continue
        r = getattr(importlib.import_module(mod_path), "router", None)
        if isinstance(r, APIRouter):
            unlisted.append(mod_path)
    assert not unlisted, (
        f"routes/ modules with a router but no ROUTER_SPECS entry: "
        f"{unlisted} — pin them in the manifest (or SKIP_DISCOVERY)"
    )
