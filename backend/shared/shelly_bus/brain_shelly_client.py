"""Brain-side client for `POST /api/mc-shelly/memory/propose`.

This is the file each brain pod imports. It's a thin httpx wrapper so
the proposal sender shape stays identical across all four brains —
no per-pod drift on serialization or auth.

Usage inside a brain pod:

    from shared.shelly_bus import ShellyMemoryProposal
    from shared.shelly_bus.brain_shelly_client import BrainShellyClient

    shelly = BrainShellyClient(
        mc_url="https://mission.risedual.ai",
        runtime_token=os.environ["MY_INGEST_TOKEN"],
    )

    await shelly.propose_memory(ShellyMemoryProposal(
        source_brain="barracuda",
        lane="crypto",
        symbol="BTC/USD",
        event_type="market_pattern",
        regime="compression",
        confidence=0.67,
        outcome="pending",
        source_id=decision_id,
        text="BTC compression with rising volume looked similar to prior breakout setups.",
    ))

Authority pin: the client only KNOWS how to send proposals. It cannot
self-certify memory; MC decides trust score and canonicalization.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from shared.shelly_bus import ShellyMemoryProposal


logger = logging.getLogger("brain_shelly_client")


class BrainShellyClient:
    """Tiny async client for the MC-Shelly proposal endpoint."""

    def __init__(
        self,
        mc_url: str,
        runtime_token: str,
        *,
        timeout: float = 10.0,
    ):
        if httpx is None:
            raise RuntimeError(
                "httpx is not installed in this brain pod's environment; "
                "install httpx>=0.27 to use BrainShellyClient"
            )
        if not mc_url:
            raise ValueError("mc_url must be a non-empty URL like 'https://mission.risedual.ai'")
        if not runtime_token:
            raise ValueError("runtime_token must be the brain's {BRAIN}_INGEST_TOKEN")
        self.mc_url = mc_url.rstrip("/")
        self.runtime_token = runtime_token
        self.timeout = timeout

    async def propose_memory(self, proposal: ShellyMemoryProposal) -> dict[str, Any]:
        """POST a proposal. Returns MC's verdict body. Raises on
        non-2xx (caller decides retry policy)."""
        endpoint = f"{self.mc_url}/api/mc-shelly/memory/propose"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                endpoint,
                json=proposal.to_doc(),
                headers={"X-Runtime-Token": self.runtime_token},
            )
            r.raise_for_status()
            return r.json()

    async def propose_many(
        self,
        proposals: list[ShellyMemoryProposal],
        *,
        max_in_flight: int = 4,
    ) -> list[dict[str, Any]]:
        """Send a batch of proposals concurrently, capped to
        `max_in_flight`. Returns the verdict list in the same order."""
        sem = asyncio.Semaphore(max_in_flight)

        async def _one(p: ShellyMemoryProposal) -> dict[str, Any]:
            async with sem:
                return await self.propose_memory(p)

        return await asyncio.gather(*(_one(p) for p in proposals))
