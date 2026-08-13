"""Mission Control — account-fit overlay (operator patch 2026-06).

NEVER changes the brain's market edge/confidence. Returns a separate,
compact sizing/execution opinion derived from live account state.
Verdicts: PASS | REDUCE | BLOCK. BLOCK is feasibility-only (duplicate
open order, zero buying power, nothing to sell) — not an opinion gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from shared.account_context import AccountSnapshot


@dataclass(frozen=True)
class AccountFit:
    score: float
    size_multiplier: float
    verdict: str   # PASS | REDUCE | BLOCK
    reasons: tuple[str, ...]

    def compact(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_account_fit(
    *,
    snapshot: AccountSnapshot,
    symbol: str,
    action: str,
    requested_notional: float,
    max_single_name_pct: float = 0.25,
    buying_power_buffer_pct: float = 0.05,
) -> AccountFit:
    symbol = symbol.upper().strip()
    action = action.upper().strip()
    requested = max(0.0, float(requested_notional))
    reasons: list[str] = []
    score = 1.0
    mult = 1.0

    pos = next((p for p in snapshot.positions if p.get("symbol") == symbol), None)
    duplicate_open = any(
        o.get("symbol") == symbol
        and o.get("side") in {action, "BUY" if action == "ADD" else action}
        and o.get("status") not in {"filled", "cancelled", "canceled", "rejected"}
        for o in snapshot.open_orders
    )
    if duplicate_open:
        return AccountFit(0.0, 0.0, "BLOCK", ("DUPLICATE_OPEN_ORDER",))

    is_entry = action in {"BUY", "ADD", "COVER"}
    if is_entry:
        if snapshot.buying_power <= 0:
            return AccountFit(0.0, 0.0, "BLOCK", ("NO_BUYING_POWER",))

        if pos and action in {"BUY", "ADD"}:
            reasons.append("EXISTING_POSITION")
            score -= 0.10
            mult *= 0.80

        reserve = max(0.0, snapshot.equity * buying_power_buffer_pct)
        spendable = max(0.0, snapshot.buying_power - reserve)
        if requested > spendable:
            if spendable <= 0:
                return AccountFit(
                    max(0.0, score - 0.50), 0.0, "BLOCK",
                    tuple(reasons + ["BUYING_POWER_RESERVE"]),
                )
            mult *= min(1.0, spendable / max(requested, 1e-9))
            score -= 0.20
            reasons.append("REDUCE_TO_BUYING_POWER")

        existing = abs(float((pos or {}).get("market_value") or 0.0))
        if snapshot.equity > 0:
            cap_value = snapshot.equity * max_single_name_pct
            room = max(0.0, cap_value - existing)
            if requested > room:
                if room <= 0:
                    return AccountFit(
                        max(0.0, score - 0.40), 0.0, "BLOCK",
                        tuple(reasons + ["SINGLE_NAME_CAP"]),
                    )
                mult *= min(1.0, room / max(requested, 1e-9))
                score -= 0.15
                reasons.append("REDUCE_SINGLE_NAME_CONCENTRATION")

    # Normal SELL is an exit; do not block due to cash/buying power.
    if action == "SELL" and not pos:
        return AccountFit(0.0, 0.0, "BLOCK", ("NO_POSITION_TO_SELL",))

    score = max(0.0, min(1.0, score))
    mult = max(0.0, min(1.0, mult))
    verdict = "PASS" if mult >= 0.999 else "REDUCE"
    return AccountFit(score, mult, verdict, tuple(reasons or ["ACCOUNT_FIT_OK"]))
