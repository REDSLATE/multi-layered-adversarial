"""Sidecar check-in surface — Portable Survival Layer companion.

Doctrine:
    Heartbeats prove a brain is ALIVE. Check-ins prove its IDENTITY.
    Every sidecar (alpha, camaro, chevelle, redeye) must POST its
    boot-time `RuntimeStamp` here so MC can answer the operator
    question "who's PROD vs preview right now?" with one query
    instead of a Mongo grep across pods.

Endpoints:
    POST /api/admin/runtime/sidecar-checkin/{brain}
        Token-authed via the per-brain `<BRAIN>_INGEST_TOKEN`
        (matches `/api/heartbeat-ping/{brain}` — same token, same
        portability story). Body: `{"stamp": {...RuntimeStamp...}}`.
        MC validates the stamp against `validate_for_prod_sidecar`,
        flags policy_hash drift vs MC's current hash, persists to
        `sidecar_checkins` (one upserted doc per runtime), and
        returns the verdict so the sidecar can self-quarantine.

    GET /api/admin/runtime/sidecar-checkin
        JWT-authed (admin). Returns one row per known brain with
        the latest stamp + verdict + freshness band. The Diagnostics
        UI renders this so the operator can see at a glance which
        sidecars are in PROD vs preview vs never-checked-in.

    GET /api/admin/runtime/sidecar-checkin/{brain}
        JWT-authed (admin). Single-brain detail (full stamp + errors).

Non-goals:
    This endpoint is OBSERVABILITY ONLY. It does NOT gate execution.
    The broker still verifies MC receipts (`shared/broker_router.py`)
    and the canonical gate still rejects bad intents
    (`shared/runtime/platform_survival.py:mc_canonical_gate`). A
    sidecar that fails its check-in here is visible to the operator
    but its trades will be independently rejected by the receipt
    seal — defense in depth.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Path, Request
from pydantic import AliasChoices, BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import DISCUSSION_PARTICIPANTS, SIDECAR_CHECKINS
from shared.runtime.platform_survival import RuntimeStamp, policy_hash


router = APIRouter(prefix="/admin/runtime", tags=["sidecar-checkin"])


# ────────────────────── Helpers ───────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _expected_token(brain: str) -> str:
    """Same shape as `shared/heartbeat_ping.py` — per-brain ingest
    token from `.env`. Returns "" if unset so misconfiguration is loud."""
    return os.environ.get(f"{brain.upper()}_INGEST_TOKEN", "") or ""


def _verdict_from_validation(validation: Dict[str, Any], mc_policy_hash: str) -> str:
    """Compress the validation errors + policy drift into one operator
    label. Verdicts:
        - "prod"           — validates clean AND policy_hash matches MC
        - "policy_drift"   — stamp says prod but its policy_hash != MC's
                             (sidecar shipped stale doctrine)
        - "preview"        — env_name != "prod" or MC URL not prod
        - "invalid"        — any other validation failure
    """
    if validation.get("ok"):
        stamp = validation.get("stamp", {})
        if stamp.get("policy_hash") != mc_policy_hash:
            return "policy_drift"
        return "prod"

    errors = set(validation.get("errors") or [])
    preview_signals = {"ENV_NOT_PROD", "MC_URL_NOT_PROD"}
    if errors & preview_signals:
        return "preview"
    return "invalid"


def _validate_stamp_dict(stamp_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce an incoming dict into a `RuntimeStamp` and run the prod
    doctrine validator. Returns the validator's `{ok, errors, stamp}`
    contract on success, or a synthetic `{ok: False, errors:
    ['STAMP_SHAPE_INVALID:...'], stamp: <raw>}` on shape failure.

    Forward-compat (2026-02-18): brain sidecars may add new optional
    fields to their RuntimeStamp before MC's dataclass learns about
    them (e.g., Alpha's `pip_fingerprint`). MC MUST tolerate unknown
    keys — otherwise every sidecar rollout that adds a stamp field
    flips every brain to verdict=INVALID until MC redeploys. We
    filter the input dict to the keys MC's dataclass knows about,
    run validation on the typed object, and persist the FULL raw
    dict (including the new fields) so the data is preserved for
    later use without forcing a lockstep redeploy.
    """
    known_fields = set(RuntimeStamp.__dataclass_fields__.keys())
    filtered = {k: v for k, v in (stamp_dict or {}).items() if k in known_fields}
    unknown_keys = sorted(k for k in (stamp_dict or {}) if k not in known_fields)
    try:
        stamp = RuntimeStamp(**filtered)
    except TypeError as e:
        return {
            "ok": False,
            "errors": [f"STAMP_SHAPE_INVALID:{e}"],
            "stamp": stamp_dict,
            "unknown_keys": unknown_keys,
        }
    result = stamp.validate_for_prod_sidecar()
    # Override the validator's typed-stamp echo with the full raw
    # incoming dict so forward-compat fields survive the round trip.
    result["stamp"] = dict(stamp_dict) if isinstance(stamp_dict, dict) else stamp_dict
    if unknown_keys:
        result["unknown_keys"] = unknown_keys
    return result


# ────────────────────── Schemas ───────────────────────────────────────


class CheckinRequest(BaseModel):
    """Sidecar check-in body.

    Doctrine (2026-05-31 — tolerant alias added):
      Brains MAY send either `stamp` (legacy, the original MC contract)
      OR `identity` (new v1 identity spec exposed in
      `memory/mc_identity_v1.py`) as the top-level body key. The
      payload shape and field names are identical inside; only the
      wrapper-key differs. MC normalizes both to `stamp` on receipt.

      Why both: Chevelle / RedEye sidecars adopted the v1 spec name
      (`identity`) before MC's POST endpoint caught up, and were
      silently 422'd. Accept both → no silent failures while the
      v1 rollout completes. Drop `identity` alias once all brains
      have settled on a single name (operator pin).
    """
    # Stored field is `stamp`; alias accepts `identity` from v1 sidecars.
    # No default — field is required. Pydantic returns 422 if neither key
    # appears in the body.
    stamp: Dict[str, Any] = Field(
        ...,
        validation_alias=AliasChoices("stamp", "identity"),
        description=(
            "Sidecar's RuntimeStamp (output of "
            "`shared/runtime/platform_survival.py:RuntimeStamp.current(...)` "
            "serialized via dataclasses.asdict). Accepted under either "
            "`stamp` (legacy) or `identity` (v1 spec) key."
        ),
    )


class CheckinResponse(BaseModel):
    ok: bool
    runtime: str
    verdict: str
    errors: list[str]
    policy_hash_match: bool
    mc_policy_hash: str
    note: str


# ────────────────────── POST: sidecar check-in ─────────────────────────


@router.post("/sidecar-checkin/{brain}", response_model=CheckinResponse)
async def post_sidecar_checkin(
    request: Request,
    body: CheckinRequest,
    brain: str = Path(..., description="brain id — alpha|camaro|chevelle|redeye"),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
) -> CheckinResponse:
    """Token-authed POST a sidecar makes on boot (and periodically) to
    declare its identity. Persists the latest stamp + verdict and
    returns the validation result so the sidecar can self-quarantine
    if it drifted.

    2026-05-30 — added source-IP + per-payload audit row. The
    upserted `sidecar_checkins` doc is great for "what's the latest
    stamp?" but it OVERWRITES on every checkin, which lost the
    Alpha-preview-pod-impersonating-prod incident. The new
    `sidecar_checkin_audit` collection appends one row per POST with
    the full validated stamp + source IP + brain's self-reported
    `process_identity` (if present). That lets the operator query
    "how many distinct (pid, hostname) tuples ever checked in as
    alpha?" and catch duplicate pods even after they've been fixed.
    """
    brain = brain.lower()
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")

    expected = _expected_token(brain)
    if not expected:
        raise HTTPException(
            status_code=500,
            detail=(
                f"no ingest token configured for {brain}; "
                f"set {brain.upper()}_INGEST_TOKEN in backend/.env"
            ),
        )
    if (x_runtime_token or "") != expected:
        raise HTTPException(status_code=401, detail="invalid token")

    validation = _validate_stamp_dict(body.stamp)
    stamp = validation.get("stamp") or body.stamp
    mc_hash = policy_hash()
    incoming_hash = (stamp or {}).get("policy_hash")
    policy_match = incoming_hash == mc_hash
    verdict = _verdict_from_validation(validation, mc_hash)

    now = _now()
    now_iso = now.isoformat()

    # ── audit-log first (append-only) ─────────────────────────────────
    # Source IP at the EDGE — defense in depth per Alpha's request.
    # Respects X-Forwarded-For if present (ingress/proxy) and falls
    # back to the direct connection. Never trusted as authoritative,
    # just useful for "which pod IP did this come from?" forensics.
    xff = request.headers.get("x-forwarded-for")
    source_ip = (
        (xff.split(",")[0].strip() if xff else None)
        or (request.client.host if request.client else None)
        or "unknown"
    )
    # The brain may include `process_identity` in the stamp now —
    # Alpha started including {pid, hostname, process_boot_at} in
    # 2026-05-30. Extract if present, store as a sibling for easy
    # querying. Backward compatible: missing → None.
    process_identity = (stamp or {}).get("process_identity")
    try:
        await db["sidecar_checkin_audit"].insert_one({
            "runtime": brain,
            "ts": now_iso,
            "ts_epoch": now.timestamp(),
            "source_ip": source_ip,
            "verdict": verdict,
            "errors": validation.get("errors", []),
            "policy_hash_match": policy_match,
            "stamp_env_name": (stamp or {}).get("env_name"),
            "stamp_git_sha": (stamp or {}).get("git_sha"),
            "stamp_pip_sha": (
                ((stamp or {}).get("pip_fingerprint") or {}).get("pip_freeze_sha256")
            ),
            "process_identity": process_identity,
        })
    except Exception:  # noqa: BLE001
        # Audit is best-effort. NEVER block a checkin on it.
        pass

    # Upsert. We keep `first_seen_at` and `checkin_count` ourselves
    # (atomic $inc + $setOnInsert) so the panel can show uptime &
    # how chatty each sidecar is.
    await db[SIDECAR_CHECKINS].update_one(
        {"runtime": brain},
        {
            "$set": {
                "runtime": brain,
                "stamp": stamp,
                "validation": {
                    "ok": validation.get("ok", False),
                    "errors": validation.get("errors", []),
                    "unknown_keys": validation.get("unknown_keys", []),
                },
                "verdict": verdict,
                "policy_hash_match": policy_match,
                "mc_policy_hash": mc_hash,
                "last_checkin_at": now_iso,
                "last_source_ip": source_ip,
            },
            "$setOnInsert": {"first_checkin_at": now_iso},
            "$inc": {"checkin_count": 1},
        },
        upsert=True,
    )

    note = {
        "prod": f"{brain} recorded as PROD sidecar; policy hash matches MC.",
        "policy_drift": (
            f"{brain} stamp validates but policy_hash differs from MC. "
            f"Redeploy the sidecar with the latest platform_survival kit."
        ),
        "preview": (
            f"{brain} appears to be a PREVIEW pod (env_name or MC URL "
            f"not prod). PROD MC will still record the check-in but "
            f"the operator dashboard will flag it amber."
        ),
        "invalid": (
            f"{brain} sent an invalid RuntimeStamp; see `errors` for "
            f"the specific doctrine failures."
        ),
    }[verdict]

    return CheckinResponse(
        ok=validation.get("ok", False) and policy_match,
        runtime=brain,
        verdict=verdict,
        errors=list(validation.get("errors", [])),
        policy_hash_match=policy_match,
        mc_policy_hash=mc_hash,
        note=note,
    )


# ────────────────────── GET: list all check-ins ────────────────────────


def _freshness(last_iso: Optional[str], now: datetime) -> Dict[str, Any]:
    """Operator-readable age band for the panel."""
    if not last_iso:
        return {"age_seconds": None, "freshness": "never"}
    try:
        t = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return {"age_seconds": None, "freshness": "never"}
    age = (now - t).total_seconds()
    if age < 300:
        band = "fresh"
    elif age < 1800:
        band = "stale"
    else:
        band = "dead"
    return {"age_seconds": round(age, 1), "freshness": band}


def _row_for_response(doc: Optional[Dict[str, Any]], brain: str, now: datetime) -> Dict[str, Any]:
    """Shape a single sidecar row for the GET response."""
    if not doc:
        return {
            "runtime": brain,
            "verdict": "never",
            "freshness": "never",
            "age_seconds": None,
            "first_checkin_at": None,
            "last_checkin_at": None,
            "checkin_count": 0,
            "policy_hash_match": False,
            "mc_policy_hash": policy_hash(),
            "stamp": None,
            "errors": [],
        }
    fresh = _freshness(doc.get("last_checkin_at"), now)
    return {
        "runtime": doc.get("runtime", brain),
        "verdict": doc.get("verdict", "invalid"),
        "freshness": fresh["freshness"],
        "age_seconds": fresh["age_seconds"],
        "first_checkin_at": doc.get("first_checkin_at"),
        "last_checkin_at": doc.get("last_checkin_at"),
        "checkin_count": doc.get("checkin_count", 0),
        "policy_hash_match": doc.get("policy_hash_match", False),
        "mc_policy_hash": doc.get("mc_policy_hash", policy_hash()),
        "stamp": doc.get("stamp"),
        "errors": (doc.get("validation") or {}).get("errors", []),
    }


@router.get("/sidecar-checkin")
async def list_sidecar_checkins(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Admin-only roster of every known brain's latest check-in.
    Always returns ONE row per `DISCUSSION_PARTICIPANTS` brain — if a
    brain has never checked in, its row is `verdict="never"` so the
    operator can spot silent sidecars immediately."""
    now = _now()
    docs_cursor = db[SIDECAR_CHECKINS].find({}, {"_id": 0})
    by_runtime: Dict[str, Dict[str, Any]] = {}
    async for d in docs_cursor:
        by_runtime[d.get("runtime", "")] = d

    rows = [_row_for_response(by_runtime.get(b), b, now) for b in DISCUSSION_PARTICIPANTS]

    return {
        "mc_policy_hash": policy_hash(),
        "checked_at": now.isoformat(),
        "rows": rows,
    }


@router.get("/sidecar-checkin/{brain}")
async def get_sidecar_checkin(
    brain: str,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Admin-only single-brain detail."""
    brain = brain.lower()
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")
    doc = await db[SIDECAR_CHECKINS].find_one({"runtime": brain}, {"_id": 0})
    return _row_for_response(doc, brain, _now())



@router.get("/sidecar-checkin/{brain}/audit")
async def get_sidecar_checkin_audit(
    brain: str,
    limit: int = 50,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Append-only per-checkin audit log for one brain.

    Returns the most recent N checkins with full source-IP +
    process_identity + env_name + git_sha + pip_freeze_sha. Used to
    forensically diagnose duplicate-pod scenarios (a preview pod
    impersonating prod, a rogue local sidecar, etc.).

    Doctrine: ADVISORY observability only. Read-only. Does NOT affect
    execution authority.
    """
    brain = brain.lower()
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")
    limit = max(1, min(500, int(limit)))
    rows = await db["sidecar_checkin_audit"].find(
        {"runtime": brain}, {"_id": 0},
    ).sort("ts_epoch", -1).limit(limit).to_list(length=limit)
    return {
        "runtime": brain,
        "count": len(rows),
        "rows": rows,
        "doctrine": "advisory_observability_only",
    }


@router.get("/sidecar-checkin/{brain}/imposter-scan")
async def get_sidecar_checkin_imposter_scan(
    brain: str,
    hours: int = 24,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Detect duplicate-pod scenarios in the audit log.

    Aggregates the last N hours of audit rows by
    (source_ip, env_name, pip_freeze_sha, process_identity hostname/pid)
    and returns the distinct buckets. If more than ONE bucket has a
    non-trivial count, two different processes have been
    authenticating as the same brain — that's the bug pattern that
    caught Alpha (preview pod posting to prod MC with the same token).

    Doctrine: ADVISORY observability only. Read-only.
    """
    brain = brain.lower()
    if brain not in DISCUSSION_PARTICIPANTS:
        raise HTTPException(status_code=404, detail=f"unknown brain {brain!r}")
    hours = max(1, min(168, int(hours)))
    cutoff = _now().timestamp() - (hours * 3600)

    rows = await db["sidecar_checkin_audit"].find(
        {"runtime": brain, "ts_epoch": {"$gte": cutoff}}, {"_id": 0},
    ).to_list(length=10000)

    # Bucket by the strongest available identity signal. Prefer
    # process_identity (Alpha 2026-05-30+), fall back to
    # (source_ip, pip_sha, env_name) for older / non-stamped brains.
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        pi = r.get("process_identity") or {}
        if pi.get("pid") and pi.get("hostname"):
            key = f"pid={pi['pid']}@{pi['hostname']}"
        else:
            key = (
                f"ip={r.get('source_ip')}"
                f"|env={r.get('stamp_env_name')}"
                f"|pip={(r.get('stamp_pip_sha') or '')[:8]}"
            )
        b = buckets.setdefault(key, {
            "count": 0, "first_ts": None, "last_ts": None,
            "env_name": r.get("stamp_env_name"),
            "source_ip": r.get("source_ip"),
            "process_identity": pi,
        })
        b["count"] += 1
        ts = r.get("ts")
        if ts:
            if b["first_ts"] is None or ts < b["first_ts"]:
                b["first_ts"] = ts
            if b["last_ts"] is None or ts > b["last_ts"]:
                b["last_ts"] = ts

    # Only flag as suspicious if 2+ buckets EACH have ≥ 3 checkins.
    # A single rogue ping shouldn't trigger; sustained dupes should.
    sustained = [b for b in buckets.values() if b["count"] >= 3]
    return {
        "runtime": brain,
        "window_hours": hours,
        "total_checkins_in_window": len(rows),
        "distinct_identities": len(buckets),
        "sustained_identities": len(sustained),
        "imposter_suspected": len(sustained) > 1,
        "buckets": list(buckets.values()),
        "doctrine": "advisory_observability_only",
    }
