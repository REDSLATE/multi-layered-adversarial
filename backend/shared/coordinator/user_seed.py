"""Seed the PARADOX coordinator's internal user row.

The coordinator mints short-lived JWTs to call its own gated endpoints.
`get_current_user` (auth.py) verifies the JWT and then looks up the
`users` row by id. This module idempotently ensures that row exists so
the coordinator's self-calls succeed without a back-door.

The coordinator user is `role=system` with no password — there is no
login path for it. It can only exist via this seeder.
"""
from __future__ import annotations

import logging

from db import db

log = logging.getLogger("risedual.paradox_coordinator")

COORDINATOR_USER_ID = "paradox-coordinator"
COORDINATOR_USER_EMAIL = "coordinator@paradox.internal"


async def ensure_coordinator_user() -> None:
    """Idempotent. Safe to call on every boot."""
    existing = await db.users.find_one({"id": COORDINATOR_USER_ID})
    if existing:
        return
    await db.users.insert_one({
        "id": COORDINATOR_USER_ID,
        "email": COORDINATOR_USER_EMAIL,
        "name": "PARADOX Coordinator",
        "role": "system",
        # No password_hash — there is no login path for this account.
        "system_account": True,
    })
    log.info("paradox_coordinator: seeded internal system user %s", COORDINATOR_USER_ID)
