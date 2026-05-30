"""mc_identity_v1.py — drop-in identity surface for brain sidecars.

Owned by: Mission Control (MC).
Spec version: v1 (2026-05-30).
Target brains: Alpha, Camaro, Chevelle, RedEye.

WHAT THIS GIVES YOU
-------------------
1. A standardized `identity` block to merge into your brain's
   GET /status response. MC's BrainHealthTile and BrainProxiedStatusTile
   read this block and render a green/red worker-eligibility chip.
2. A lifecycle log line ("STARTED" / "NOT STARTED" with named
   missing env vars) emitted exactly once at boot.
3. A composite boolean `checkin_worker_eligible` so the operator
   can answer "is this brain wired correctly?" in <1 second from
   the MC dashboard, no curl/grep required.

WHY THIS MATTERS
----------------
Two prod incidents (RedEye 7h gap, Alpha 2h-sovereign / 10h-alpaca-ping)
were "guess which env var is missing" hunts because the upstream-truth
wasn't surfaced anywhere. This module is the cure: ship it, name the
failure inline.

CONTRACT (do not deviate — MC's chip code reads these field names)
------------------------------------------------------------------
GET /status response MUST include `identity` with these keys:

    identity:
      app_name:            str             # human label e.g. "alpha"
      env_name:            str             # "prod" | "preview" | ...
      git_sha:             str | None      # short sha
      broker_mode:         str | None      # "paper" | "live"
      sidecar_version:     str             # bump when YOU release

      # Check-in pair: brain → MC periodic ping (POST /api/admin/runtime/sidecar-checkin)
      mc_url_set:          bool            # MC_URL env var present
      ingest_token_set:    bool            # MC_INGEST_TOKEN env var present

      # Heartbeat pair: MC → brain opinion delivery (or brain polling MC)
      mc_base_url_set:     bool            # MC_BASE_URL env var present
      heartbeat_token_set: bool            # HEARTBEAT_TOKEN env var present

      # Composite (operator's at-a-glance answer)
      checkin_worker_eligible: bool        # ALL four booleans above are True

HOW TO INTEGRATE (copy/paste, 3 steps)
--------------------------------------

# STEP 1 — drop this file into your brain repo (e.g. sidecar/mc_identity_v1.py)

# STEP 2 — in your boot path (whatever runs once at startup):
#
#     from mc_identity_v1 import build_identity_block, log_lifecycle, start_checkin_worker
#
#     identity = build_identity_block(app_name="alpha", sidecar_version="2.4.1")
#     log_lifecycle(identity)
#     if identity["checkin_worker_eligible"]:
#         start_checkin_worker()   # your existing periodic ping function

# STEP 3 — in your /status route handler:
#
#     @app.get("/status")
#     async def status():
#         identity = build_identity_block(app_name="alpha", sidecar_version="2.4.1")
#         return {
#             "identity": identity,
#             "seats":           ...,   # your existing fields
#             "heartbeat":       ...,
#             "governor_emitter": ...,
#             "data_keys":       ...,
#             "neuro_engine":    ...,
#             "intents":         ...,
#         }

ENV VARS YOUR DEPLOYMENT MUST SET (all 4 required for ELIGIBLE)
---------------------------------------------------------------
    MC_URL              = https://mission.risedual.ai           # MC's external base
    MC_INGEST_TOKEN     = <token MC accepts for THIS brain>     # check-in auth
    MC_BASE_URL         = https://mission.risedual.ai           # same as MC_URL; kept separate so each pair fails independently
    HEARTBEAT_TOKEN     = <token MC accepts on the opinion path> # opinion stream auth

If any one is missing → `checkin_worker_eligible` = False and the MC
dashboard chip turns RED with the missing env var named inline.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional


logger = logging.getLogger("brain.mc_identity")

# Prevent the lifecycle log from firing more than once per process
# (e.g. if your app reloads modules in dev).
_LIFECYCLE_LOGGED = threading.Event()


def _get(name: str) -> Optional[str]:
    """Read an env var; treat empty string as unset (common deploy bug)."""
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def build_identity_block(
    *,
    app_name: str,
    sidecar_version: str,
    env_name: Optional[str] = None,
    git_sha: Optional[str] = None,
    broker_mode: Optional[str] = None,
) -> dict:
    """Build the identity block MC's chip reads.

    Args:
        app_name:        Human label for this brain (e.g. "alpha").
        sidecar_version: Your sidecar's release version.
        env_name:        Defaults to ENV_NAME or "unknown".
        git_sha:         Defaults to GIT_SHA env var.
        broker_mode:     "paper" | "live" | None.
    """
    mc_url = _get("MC_URL")
    ingest_token = _get("MC_INGEST_TOKEN")
    mc_base_url = _get("MC_BASE_URL")
    heartbeat_token = _get("HEARTBEAT_TOKEN")

    mc_url_set = mc_url is not None
    ingest_token_set = ingest_token is not None
    mc_base_url_set = mc_base_url is not None
    heartbeat_token_set = heartbeat_token is not None

    checkin_worker_eligible = (
        mc_url_set
        and ingest_token_set
        and mc_base_url_set
        and heartbeat_token_set
    )

    return {
        "app_name": app_name,
        "env_name": env_name or _get("ENV_NAME") or "unknown",
        "git_sha": git_sha or _get("GIT_SHA"),
        "broker_mode": broker_mode or _get("BROKER_MODE"),
        "sidecar_version": sidecar_version,
        # Check-in pair
        "mc_url_set": mc_url_set,
        "ingest_token_set": ingest_token_set,
        # Heartbeat pair
        "mc_base_url_set": mc_base_url_set,
        "heartbeat_token_set": heartbeat_token_set,
        # Composite
        "checkin_worker_eligible": checkin_worker_eligible,
    }


def log_lifecycle(identity: dict) -> None:
    """Emit exactly ONE lifecycle log line at boot.

    Format:
        "mc_checkin worker STARTED — periodic check-in every 300s
         (MC_URL set, MC_INGEST_TOKEN set, MC_BASE_URL set, HEARTBEAT_TOKEN set)"

        OR

        "mc_checkin worker NOT STARTED — missing env vars:
         MC_URL, MC_INGEST_TOKEN"

    The NOT STARTED branch ALWAYS names the missing var(s). This is the
    contract MC's tripwire test asserts. Don't change the format without
    coordinating with MC's testing — the operator's grep workflow depends
    on it.
    """
    if _LIFECYCLE_LOGGED.is_set():
        return
    _LIFECYCLE_LOGGED.set()

    pairs = [
        ("MC_URL", identity["mc_url_set"]),
        ("MC_INGEST_TOKEN", identity["ingest_token_set"]),
        ("MC_BASE_URL", identity["mc_base_url_set"]),
        ("HEARTBEAT_TOKEN", identity["heartbeat_token_set"]),
    ]
    if identity["checkin_worker_eligible"]:
        set_clause = ", ".join(f"{name} set" for name, _ in pairs)
        logger.info(
            "mc_checkin worker STARTED — periodic check-in every 300s (%s)",
            set_clause,
        )
    else:
        missing = [name for name, present in pairs if not present]
        logger.warning(
            "mc_checkin worker NOT STARTED — missing env vars: %s",
            ", ".join(missing) if missing else "unknown",
        )


# Sentinel for callers that just want a "yes/no — should I start my
# worker?" question without reading the dict.
def is_checkin_eligible() -> bool:
    return build_identity_block(
        app_name="probe", sidecar_version="probe",
    )["checkin_worker_eligible"]


# ─────────────────────────────────────────────────────────────────────
# OPTIONAL: minimal example check-in worker stub. You almost certainly
# already have one; this is here only to show the integration shape.
# Delete or replace with your existing worker.
# ─────────────────────────────────────────────────────────────────────

def start_checkin_worker(
    interval_s: int = 300,
    *,
    on_tick=None,
    on_error=None,
) -> None:
    """Spawn a background thread that pings MC every `interval_s` seconds.

    This is a SKELETON. Replace `on_tick` with your actual check-in
    HTTP call. Leaves the worker idle if env vars are missing — same
    decision tree `log_lifecycle` reports.
    """
    if not is_checkin_eligible():
        logger.warning("start_checkin_worker: NOT STARTED (env vars missing)")
        return

    import time

    def _loop():
        while True:
            try:
                if on_tick is not None:
                    on_tick()
            except Exception as e:  # noqa: BLE001
                if on_error is not None:
                    try:
                        on_error(e)
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning("mc_checkin: periodic ping failed: %r", e)
            time.sleep(interval_s)

    t = threading.Thread(target=_loop, daemon=True, name="mc_checkin_worker")
    t.start()
    logger.info("mc_checkin worker thread spawned: name=%s interval=%ss", t.name, interval_s)


# ─────────────────────────────────────────────────────────────────────
# Self-test: run `python mc_identity_v1.py` to validate locally.
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    iden = build_identity_block(app_name="selftest", sidecar_version="0.0.0")
    log_lifecycle(iden)
    print("identity block:")
    for k, v in iden.items():
        print(f"  {k:28} {v!r}")
    print()
    print(f"checkin_worker_eligible: {iden['checkin_worker_eligible']}")
