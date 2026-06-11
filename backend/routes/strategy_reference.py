"""Live Doctrine Reference — read-only operator dashboard backend.

Mounts at `/api/admin/doctrine-reference` (auth/seat checks handled at
the router-include level in `server.py`). Reads `DOCTRINE_CARDS` and
`_DOCTRINE_FN_MAP` directly from the live doctrine modules so the page
can never drift from committed code.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from shared.doctrine.brain_sidecars import DOCTRINE_CARDS as GENERIC_CARDS
from shared.doctrine.large_cap_doctrine import DOCTRINE_CARDS as LARGE_CAP_CARDS
from shared.doctrine.strategy_doctrines import DOCTRINE_CARDS as STRATEGY_CARDS

router = APIRouter(prefix="/admin", tags=["doctrine-reference"])


def _load_all_cards() -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    sources = [
        (STRATEGY_CARDS, "strategy_doctrines"),
        (LARGE_CAP_CARDS, "large_cap_doctrine"),
        (GENERIC_CARDS, "brain_sidecars"),
    ]
    for source, module_name in sources:
        for sid, card in source.items():
            if sid in merged:
                raise RuntimeError(
                    f"Duplicate strategy_id '{sid}' across doctrine modules"
                )
            # Shallow copy + injected source_module so the dashboard can
            # show provenance without mutating the registry in-place.
            merged[sid] = {**card, "strategy_id": sid, "source_module": module_name}
    return merged


@router.get("/doctrine-reference")
async def doctrine_reference() -> Dict[str, Any]:
    """Full payload of every operator card, merged across doctrine modules.

    No caching — always reflects committed code.
    """
    all_cards = _load_all_cards()
    return {
        "strategies": list(all_cards.values()),
        "count": len(all_cards),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/doctrine-reference/index")
async def doctrine_index() -> Dict[str, Any]:
    """Lightweight index — sidebar / navigation payload."""
    all_cards = _load_all_cards()
    items: List[Dict[str, str]] = [
        {
            "strategy_id": sid,
            "title": c["title"],
            "category": c["category"],
            "lane": c["lane"],
            "doctrine_version": c["doctrine_version"],
            "source_module": c["source_module"],
        }
        for sid, c in all_cards.items()
    ]
    return {
        "count": len(items),
        "strategies": items,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/doctrine-reference/{strategy_id}")
async def doctrine_card(strategy_id: str) -> Dict[str, Any]:
    """Single operator card. Pulled mid-trade for quick reference."""
    all_cards = _load_all_cards()
    card = all_cards.get(strategy_id)
    if not card:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Strategy '{strategy_id}' not found. "
                f"Available: {list(all_cards.keys())}"
            ),
        )
    return card
