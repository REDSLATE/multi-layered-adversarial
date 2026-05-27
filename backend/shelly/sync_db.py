"""Sync pymongo client for the Shelly memory/reasoning layer.

Doctrine pin (pass #13 follow-up):
    Shelly intentionally runs OUTSIDE the async hot path.

    The rest of the MC backend uses motor (`db.py`) for the request /
    gate-chain critical path. Shelly is a memory + reasoning layer
    that:
      * does NOT participate in gate evaluation
      * does NOT carry execution authority
      * does NOT need sub-millisecond reads

    Running Shelly on sync pymongo keeps it isolated:
      * Tests don't fight motor's event-loop binding
      * `after_brain_receipt` can be called from anywhere (sync, async,
        background thread) without `asyncio.to_thread` gymnastics
      * A Shelly DB hiccup cannot block the live trading hot path

    Same Mongo cluster, same DB, separate `MongoClient` instance.
    pymongo is already installed as a transitive dep of motor — no
    new requirements.
"""
from __future__ import annotations

import os

from pymongo import MongoClient
from pymongo.database import Database


_client: MongoClient | None = None
_db: Database | None = None


def get_db() -> Database:
    """Lazy-init the sync client. Reuses a single MongoClient across
    the process so we don't open a connection per call."""
    global _client, _db
    if _db is None:
        _client = MongoClient(os.environ["MONGO_URL"])
        _db = _client[os.environ["DB_NAME"]]
    return _db
