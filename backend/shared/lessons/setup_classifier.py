"""Setup classifier — derive a stable `setup_id` from an intent's
research evidence + action.

A setup is the (strategy_id, direction) pair that the brain effectively
played. We use this label as the grouping key in Brain Report Cards
so the operator can answer questions like:

    "How does Hellcat do on crypto_breakdown_v1 SELLs?"
    "Is Camino's `large_cap_momentum_v1 BUY` track record any good?"

When research signals are missing or pure-HOLD (e.g. a brain emitted
a contrarian trade against the lab), we fall back to a synthetic
setup id keyed off the action alone so the lesson still groups
meaningfully.

Doctrine guard: classifier is PURE — no I/O, no Mongo lookups, no
randomness. Same inputs → same setup_id forever. Otherwise the
report-card history would shift under us as we tweaked classifier
rules.
"""
from __future__ import annotations

from typing import Optional


def _strongest_non_hold(signals: list[dict]) -> Optional[dict]:
    """Pick the non-HOLD signal with the highest score; tie-break by
    listed order so a future re-ordering of `STRATEGIES` doesn't
    silently change setup ids."""
    candidates = [s for s in (signals or []) if s.get("direction") in ("BUY", "SELL")]
    if not candidates:
        return None
    candidates.sort(key=lambda s: float(s.get("score") or 0.0), reverse=True)
    return candidates[0]


def classify_setup(action: str, research_signals: Optional[list[dict]]) -> str:
    """Return a stable string id describing what setup the brain
    effectively played.

    Examples:
      ("BUY",  [{"strategy_id":"large_cap_momentum_v1","direction":"BUY", ...}])
          → "large_cap_momentum_v1:BUY"
      ("SELL", [{"strategy_id":"crypto_breakdown_v1","direction":"SELL", ...}])
          → "crypto_breakdown_v1:SELL"
      ("BUY",  [{"...","direction":"HOLD",...}])                # research said HOLD
          → "contrarian:BUY"
      ("BUY",  [])                                              # no research
          → "unscored:BUY"
      ("HOLD", any)
          → "abstain"

    A brain that disagrees with research (action ≠ strongest research
    direction) gets the `contrarian:<action>` label so the report-card
    can grade contrarian conviction separately from agreement plays.
    """
    a = (action or "").upper().strip()
    if a == "HOLD":
        return "abstain"

    sig = _strongest_non_hold(research_signals or [])
    if sig is None:
        # No strategy fired with a directional view — brain went on
        # its own.
        return f"unscored:{a}"

    direction = sig.get("direction")
    strategy_id = sig.get("strategy_id") or "unknown_strategy"
    if direction == a:
        return f"{strategy_id}:{a}"
    return f"contrarian:{a}"
