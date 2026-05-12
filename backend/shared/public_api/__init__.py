"""Public API namespace — Direction C ("two faces, one brain").

Mission Control becomes the silent intelligence backend for the public
site (risedual.ai). risedual.ai keeps its own auth, Stripe, credits,
and tier gating; MC just returns sanitized JSON in shapes the public
frontend already expects.

Auth: trust + tier passthrough.
    X-RiseDual-Token       — opaque bearer, set by env on MC, kept on
                             risedual.ai's backend and forwarded.
    X-RiseDual-User-Tier   — one of {free, starter, pro, pro_max}.
                             MC enforces "locked rows" for free/starter
                             on tier-gated endpoints (digest, etc.) so
                             risedual.ai's frontend doesn't have to.

Important — what MC does NOT do:
  * No Stripe / billing / credit ledger. risedual.ai already has it.
  * No user accounts. risedual.ai owns identity.
  * No PCI scope. MC never sees credit card data or user emails.
  * Operator JWT and runtime tokens are NOT accepted here — separate
    door, separate lock.
"""
from .router import router

__all__ = ["router"]
