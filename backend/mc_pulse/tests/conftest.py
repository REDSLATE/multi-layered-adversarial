"""Seed `/app/backend/.env` into `os.environ` for the pulse tests.
Mirror of `mc_arbiter/tests/conftest.py`."""
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
