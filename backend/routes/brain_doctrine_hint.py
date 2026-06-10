"""Brain doctrine hint endpoint — read-only.

Doctrine pin (2026-02-18):
    A scaffolding surface brains MAY consult before emitting an
    intent. Returns:
      • candidate doctrine versions for a given symbol/snapshot
      • current LEARNING / WATCHING / CANDIDATE state of each
      • a recommended `emit_semantic` hint string

    NEVER mutates state. NEVER blocks execution. The brain's
    sidecar process owns its own decision; this endpoint is only
    a *suggestion* tied to the live doctrine state.

    Authoritative invariants (mirrored in docstring + doctrine_note
    payload field so any reader can verify):
      * HOLD never becomes trade (MC will still reject HOLDs at
        the gate chain's `action_routable` step).
      * MC never blocks based on LEARNING state.
      * Brains own emit shape; MC only stamps and gates.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import DOCTRINE_SIDECARS, RUNTIMES
from runtime_auth import verify_runtime_token


router = APIRouter(prefix="/admin/brain", tags=["brain-diagnose"])


# Same thresholds as `shared/doctrine/promotion.py`. Duplicated as
# constants to avoid an import cycle on the router-side response.
_MIN_SAMPLES = 100
_EXPECTANCY_PROMOTION_FLOOR = 0.30
_EXPECTANCY_RETIREMENT_FLOOR = -0.10
_MAX_DD_RETIREMENT_FLOOR = 8.0


def _verdict_for(samples: int, expectancy_R: Optional[float],
                 max_dd_R: Optional[float]) -> str:
    if samples < _MIN_SAMPLES:
        return "LEARNING"
    if expectancy_R is not None and expectancy_R < _EXPECTANCY_RETIREMENT_FLOOR:
        return "CANDIDATE_RETIREMENT"
    if max_dd_R is not None and max_dd_R >= _MAX_DD_RETIREMENT_FLOOR:
        return "CANDIDATE_RETIREMENT"
    if expectancy_R is not None and expectancy_R >= _EXPECTANCY_PROMOTION_FLOOR:
        return "CANDIDATE_PROMOTION"
    return "WATCHING"


def _emit_semantic_for(verdict: str) -> str:
    """Map verdict band → brain-side emit hint."""
    return {
        "LEARNING":            "emit_with_downsize",
        "WATCHING":            "emit_with_downsize",
        "CANDIDATE_PROMOTION": "emit_normal",
        "CANDIDATE_RETIREMENT": "informational_only",
    }.get(verdict, "emit_with_downsize")


def _candidate_doctrines_for(snapshot: dict) -> list[str]:
    """Compose the list of doctrine_versions this snapshot is eligible
    to be evaluated under. Order is preference (most specific first)."""
    lane = (snapshot.get("lane") or "").lower()
    if lane == "crypto":
        return ["crypto_sidecar_v1"]
    candidates: list[str] = []
    strategy = (snapshot.get("strategy") or "").lower()
    band = (snapshot.get("market_cap_band") or "").lower()
    if strategy == "large_cap" or band in ("large", "mega"):
        candidates.append("large_cap_equity_v1")
    if strategy == "gap_and_go":
        candidates.append("gap_and_go_v1")
    if strategy == "micro_pullback":
        candidates.append("micro_pullback_v1")
    candidates.append("small_account_sidecar_v1")
    return candidates


async def _live_state_for(doctrine_version: str, lane: str) -> dict:
    """Compute live (samples, expectancy_R, max_dd_R, verdict) for a
    doctrine version. Reads `doctrine_sidecars` rows with outcome
    joins — mirrors `promotion.promotion_status` but slimmed down.

    Defensive notes (2026-06-10):

      * The row cap is 5_000 (down from 50_000). No doctrine carries
        anywhere near 5K outcome-joined rows today, and the verdict
        bands only need ≥ `_MIN_SAMPLES = 100` to leave LEARNING.
        Pulling 50K per doctrine × 4 doctrines per hint call was
        unnecessary I/O and contributed to a sporadic timeout flake
        on `test_doctrine_hint_returns_candidates_for_large_cap`
        observed under full-suite load on 2026-06-10.
      * The query sorts by `_id` descending so when truncation hits
        we keep the FRESHEST outcomes, which is what the verdict
        bands actually need (stale rows from a year ago aren't
        informative about current expectancy).
    """
    rows = await db[DOCTRINE_SIDECARS].find(
        {
            "doctrine_version": doctrine_version,
            "lane": lane,
            "outcome_join": {"$exists": True},
        },
        {"_id": 1, "outcome_join": 1},
    ).sort("_id", -1).to_list(5_000)

    pnls = []
    for r in rows:
        oj = r.get("outcome_join") or {}
        pnl = oj.get("pnl_usd")
        if isinstance(pnl, (int, float)):
            pnls.append(float(pnl))

    n = len(pnls)
    if n == 0:
        return {
            "samples": 0,
            "expectancy_R": None,
            "max_drawdown_R": None,
            "win_rate": None,
            "verdict": "LEARNING",
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_loss_abs = abs(sum(losses) / len(losses)) if losses else 0.01
    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    exp_R = win_rate * (avg_win / avg_loss_abs) - loss_rate

    cur_dd = 0.0
    max_dd = 0.0
    for p in pnls:
        if p < 0:
            cur_dd += abs(p) / avg_loss_abs
            if cur_dd > max_dd:
                max_dd = cur_dd
        else:
            cur_dd = 0.0

    return {
        "samples": n,
        "expectancy_R": round(exp_R, 4),
        "max_drawdown_R": round(max_dd, 4),
        "win_rate": round(win_rate, 4),
        "verdict": _verdict_for(n, exp_R, max_dd),
    }


@router.get("/doctrine-hint")
async def doctrine_hint(
    symbol: str = Query(..., min_length=1, max_length=24),
    lane: str = Query(default="equity", description="equity or crypto"),
    strategy: Optional[str] = Query(default=None),
    market_cap_band: Optional[str] = Query(default=None),
    x_runtime_token: Optional[str] = Header(default=None, alias="X-Runtime-Token"),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Read-only doctrine state hint a brain MAY consult before emitting.

    Authn:
      * Accepts either an operator JWT (admin auth dependency) OR an
        `X-Runtime-Token` from any brain — sidecars use the runtime
        token so they don't need operator creds.

    Returns:
      * `candidate_doctrines` — doctrine_versions this snapshot is
        eligible to be scored under (order: preference).
      * `state` — live (samples, expectancy_R, max_dd_R, verdict)
        per candidate doctrine.
      * `recommended_emit_semantic` — hint string for the brain.
      * `doctrine_note` — invariants the brain must still honor.

    The brain may safely ignore this endpoint; the gate chain still
    enforces every invariant regardless.
    """
    # Either auth path is fine. If the runtime token is supplied, we
    # accept any of the 4 brain tokens. The operator JWT dependency
    # above will already have validated for admins.
    if x_runtime_token:
        matched = False
        for rt in RUNTIMES:
            try:
                verify_runtime_token(rt, x_runtime_token)
                matched = True
                break
            except HTTPException:
                continue
        if not matched:
            raise HTTPException(status_code=401, detail="invalid runtime token")

    snapshot = {
        "symbol": symbol.upper().strip(),
        "lane": lane.lower().strip(),
        "strategy": (strategy or "").lower().strip() or None,
        "market_cap_band": (market_cap_band or "").lower().strip() or None,
    }
    candidates = _candidate_doctrines_for(snapshot)

    state_by_doctrine = {}
    for dv in candidates:
        # Defense in depth (2026-06-10): wrap each per-doctrine state
        # computation so a slow or failing query against ONE doctrine
        # version doesn't 500 the entire hint call. The brain can
        # still consult the others; a degraded state row is preferable
        # to no answer at all.
        try:
            state_by_doctrine[dv] = await _live_state_for(dv, snapshot["lane"])
        except Exception as e:  # noqa: BLE001
            state_by_doctrine[dv] = {
                "samples": 0,
                "expectancy_R": None,
                "max_drawdown_R": None,
                "win_rate": None,
                "verdict": "LEARNING",
                "error": str(e)[:120],
            }

    # The recommended semantic uses the FIRST candidate's verdict (the
    # most-specific match). A brain that disagrees with the dispatch is
    # free to look at the others.
    primary_verdict = state_by_doctrine[candidates[0]]["verdict"]
    return {
        "symbol": snapshot["symbol"],
        "lane": snapshot["lane"],
        "candidate_doctrines": candidates,
        "primary_doctrine": candidates[0],
        "primary_verdict": primary_verdict,
        "recommended_emit_semantic": _emit_semantic_for(primary_verdict),
        "state_by_doctrine": state_by_doctrine,
        "doctrine_note": (
            "HINT ONLY. MC never blocks based on LEARNING state. "
            "HOLD never becomes trade (gate chain rejects HOLDs at "
            "`action_routable`). Brain owns its emit decision; this "
            "endpoint informs the brain, never compels it."
        ),
    }
