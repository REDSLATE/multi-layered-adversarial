"""Runtime flags read from env.

Doctrine (2026-02-17, rev3): seat policy is the only authority gate.
Brain-named enforce flags have been retired — authority does not
depend on which brain holds which seat, so flags scoped to brain
identity (PHASE6_ENFORCE_ENABLED, CAMARO_EXECUTOR_ENFORCE_ENABLED,
CHEVELLE_AUTHORITY_ENABLED, REDEYE_OPPONENT_ENFORCE_ENABLED) are no
longer read or surfaced. Only system-wide flags survive.
"""
import os
from fastapi import APIRouter, Depends

from auth import get_current_user
from namespaces import ROLES, RUNTIMES

router = APIRouter(prefix="/admin/flags", tags=["flags"])


def _b(name: str) -> bool:
    return os.environ.get(name, "false").lower() == "true"


def get_flags_snapshot() -> dict:
    return {
        "deploy_mode": os.environ.get("DEPLOY_MODE", "observation"),
        "broker_live_order_enabled": _b("BROKER_LIVE_ORDER_ENABLED"),
        # Legacy field kept as an empty dict for one deprecation cycle
        # so any old frontend bundle reading `enforce_flags.*` doesn't
        # blank-render on a missing key. New consumers MUST treat seat
        # policy as the source of authority.
        "enforce_flags": {},
        "roles": {rt: ROLES[rt] for rt in RUNTIMES},
        "doctrine": (
            "Authority lives on SEATS, not brains. Any eligible brain "
            "may hold any seat. Performance attaches to "
            "(lane, seat, doctrine_version) — never to brain identity. "
            "Promotion / retirement targets the seat doctrine. "
            "Holders rotate."
        ),
    }


@router.get("")
async def list_flags(_user: dict = Depends(get_current_user)):
    return get_flags_snapshot()
