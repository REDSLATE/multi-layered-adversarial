"""Operator endpoint: intent stream split by runtime_origin.

Both preview and prod pods can write to the same `shared_intents`
collection (we run on one Mongo cluster). To keep the streams
attributable WITHOUT moving to separate collections, every intent
posted by the in-process brain runner now carries:

    evidence.runtime_origin   — RISEDUAL_RUNTIME_ORIGIN or hostname
    evidence.pod_hostname     — socket.gethostname() of the writer pod
    evidence.env_name_emit    — the RISEDUAL_ENV value at write time

This module surfaces those discriminators in a single read-only
operator endpoint so the dashboard can show "intents from THIS pod"
vs "intents from anywhere".

NEVER mutates intents. NEVER gates trading.
"""
from __future__ import annotations

import os
import socket
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


router = APIRouter(prefix="/admin/intents", tags=["intents-origin"])


def _local_origin() -> dict:
    """The discriminator values that THIS pod is writing right now."""
    try:
        hostname = socket.gethostname() or "unknown"
    except Exception:  # noqa: BLE001
        hostname = "unknown"
    return {
        "runtime_origin": os.environ.get(
            "RISEDUAL_RUNTIME_ORIGIN", "").strip() or hostname,
        "pod_hostname": hostname,
        "env_name_emit": os.environ.get(
            "RISEDUAL_ENV", os.environ.get("ENV", "unknown"),
        ),
    }


@router.get("/origins")
async def list_intent_origins(
    limit_per_origin: int = Query(default=3, ge=1, le=20),
    _user: dict = Depends(get_current_user),
):
    """Group recent intents by (runtime_origin, env_name_emit) so
    the operator can see at a glance which pods are writing to
    shared_intents.

    Response shape:
        {
            "local": {runtime_origin, pod_hostname, env_name_emit},
            "groups": [
                {
                    "runtime_origin": "...",
                    "env_name_emit": "...",
                    "count_24h": N,
                    "sample": [{intent_id, brain, symbol, action, ts}, ...]
                },
                ...
            ]
        }
    """
    pipeline = [
        {"$match": {
            "evidence.runtime_origin": {"$exists": True},
        }},
        {"$group": {
            "_id": {
                "origin": "$evidence.runtime_origin",
                "env": "$evidence.env_name_emit",
            },
            "count": {"$sum": 1},
            "latest_ts": {"$max": "$ingest_ts"},
            "sample_ids": {"$push": {
                "intent_id": "$intent_id",
                "brain": "$stack",
                "symbol": "$symbol",
                "action": "$action",
                "ts": "$ingest_ts",
            }},
        }},
        {"$sort": {"latest_ts": -1}},
        {"$limit": 20},
    ]
    groups: list[dict] = []
    async for row in db.shared_intents.aggregate(pipeline):
        key = row.get("_id") or {}
        sample = row.get("sample_ids") or []
        # Take the most recent N entries from the push'd array.
        sample.sort(key=lambda r: r.get("ts") or "", reverse=True)
        groups.append({
            "runtime_origin": key.get("origin"),
            "env_name_emit": key.get("env"),
            "count": int(row.get("count", 0)),
            "latest_ts": row.get("latest_ts"),
            "sample": sample[:limit_per_origin],
        })
    return {"local": _local_origin(), "groups": groups, "n_groups": len(groups)}


@router.get("/by-origin")
async def intents_by_origin(
    origin: str = Query(..., description="`runtime_origin` to filter by"),
    limit: int = Query(default=50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    """Most recent N intents written by a specific pod."""
    rows = await db.shared_intents.find(
        {"evidence.runtime_origin": origin},
        {"_id": 0, "intent_id": 1, "stack": 1, "symbol": 1, "action": 1,
         "confidence": 1, "ingest_ts": 1, "evidence.env_name_emit": 1,
         "evidence.pod_hostname": 1},
    ).sort("ingest_ts", -1).to_list(limit)
    return {"origin": origin, "items": rows, "count": len(rows)}
