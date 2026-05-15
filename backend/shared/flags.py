"""Runtime flags read from env. Observation-only enforcement.
Flags are read-only via API in observation mode."""
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
        "enforce_flags": {
            "alpha_phase6_enforce_enabled": _b("PHASE6_ENFORCE_ENABLED"),
            "camaro_executor_enforce_enabled": _b("CAMARO_EXECUTOR_ENFORCE_ENABLED"),
            "chevelle_authority_enabled": _b("CHEVELLE_AUTHORITY_ENABLED"),
            "redeye_opponent_enforce_enabled": _b("REDEYE_OPPONENT_ENFORCE_ENABLED"),
        },
        "roles": {rt: ROLES[rt] for rt in RUNTIMES},
        "doctrine": (
            "observation-only — execution authority disabled across all runtimes. "
            "Only Alpha has hands. Camaro has teeth. Chevelle has the keys. "
            "REDEYE argues the contrary case."
        ),
    }


@router.get("")
async def list_flags(_user: dict = Depends(get_current_user)):
    return get_flags_snapshot()
