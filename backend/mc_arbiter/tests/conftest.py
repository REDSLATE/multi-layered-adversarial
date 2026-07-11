"""Conftest for mc_arbiter tests — seeds `/app/backend/.env` into
`os.environ` before any test module imports the app modules that
read env at import time (`db.py`, `shared.*`).

Mirrors the pattern in `/app/backend/tests/conftest.py` — see that
file for the 2026-06-07 rationale about full env parity.
"""
from __future__ import annotations

import os

_BE_ENV = "/app/backend/.env"
if os.path.exists(_BE_ENV):
    with open(_BE_ENV) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            os.environ.setdefault(_k, _v)
