"""Governor consumer for the witness credibility ledger.

Doctrine pin (2026-02-19, operator directive):
    The `external_source_credibility` ledger (owned by Verifier) has
    always had a `status` field with three tiers: UNTRUSTED →
    WATCHLIST → TRUSTED. But no code on the execution side has ever
    consumed that status. `influence_allowed` (bool) is the only
    gate that exists in the intake path, and that's binary.

    This module is the missing per-tier scale — the numeric answer
    to "how much modifier does a WATCHLIST source get vs TRUSTED?"
    Kept in ONE place so tests, docs, and Governor code cannot
    disagree.

    Read-only against the ledger. Never writes. Never promotes.
    Verifier owns tier transitions; this module only reads and maps.

Default-hostile:
    Any status the ledger doesn't recognize, any source that has no
    ledger row yet, any read error → 0.0. A brand-new witness
    source cannot silently pick up influence just because it exists.

    2026-02-19 additional guard: since the Verifier's resolver
    runner was excised in the simplification pass, no code path
    refreshes credibility rows automatically anymore. A previously
    TRUSTED source could therefore keep its ceiling forever unless
    the operator manually re-promotes it. This module now clamps
    to 0.0 when `updated_at` is older than `WITNESS_STALE_MAX_HOURS`
    (default 72h). Missing `updated_at` is treated as stale.

Modifier interpretation (per operator pin):
    The float returned here is the CEILING influence the witness can
    contribute to Governor sizing when its stance is judged
    orthogonal to the brain thesis. Non-orthogonal (agreement / echo)
    signals still receive 0.0 — those numbers only apply when the
    witness saw something the brains did not.

    UNTRUSTED  → 0.00  (short-circuit, cannot influence sizing)
    WATCHLIST  → 0.05  (5 % ceiling — proving period, small dial)
    TRUSTED    → 0.15  (15 % ceiling — earned position)
    STALE      → 0.00  (row updated_at > WITNESS_STALE_MAX_HOURS ago)

    These are DRAFTS. First real promotion event will produce
    observable alpha/drawdown numbers that make the ceilings
    concrete. Tunable via `WITNESS_MODIFIER_*` env vars without a
    code push, per the tunability doctrine the sizing gate already
    follows.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import EXTERNAL_SOURCE_CREDIBILITY


logger = logging.getLogger("risedual.witness_influence")


# Baseline ceilings — safe if env is missing entirely.
_DEFAULT_UNTRUSTED = 0.00
_DEFAULT_WATCHLIST = 0.05
_DEFAULT_TRUSTED = 0.15
_DEFAULT_STALE_MAX_HOURS = 72  # 3 days


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        logger.warning(
            "witness_influence: bad %s=%r, using %s", name, raw, default,
        )
        return default


def _stale_max_hours() -> float:
    """Env-tunable staleness threshold. Any row whose `updated_at`
    is older than this is clamped to 0.0. Missing/empty → default.
    Negative or unparseable → default (never allow 0/disable, since
    the whole point is to keep frozen rows from bleeding through).
    """
    raw = os.environ.get("WITNESS_STALE_MAX_HOURS")
    if not raw:
        return float(_DEFAULT_STALE_MAX_HOURS)
    try:
        v = float(raw)
        if v <= 0:
            return float(_DEFAULT_STALE_MAX_HOURS)
        return v
    except (TypeError, ValueError):
        return float(_DEFAULT_STALE_MAX_HOURS)


def _is_stale(updated_at_iso: Optional[str]) -> bool:
    """Return True when the row is missing an `updated_at` or the
    timestamp is older than the staleness window. Any parse error
    is treated as stale — default-hostile."""
    if not updated_at_iso:
        return True
    try:
        # Support "...Z" trailing form as well as offset-aware ISO.
        ts = datetime.fromisoformat(str(updated_at_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_hours = (now - ts).total_seconds() / 3600.0
    return age_hours > _stale_max_hours()


def modifier_for_status(status: Optional[str]) -> float:
    """Pure function — no I/O. Maps a status label to its ceiling float.

    Exists as a separate function so unit tests can pin the table
    without hitting Mongo, and so callers that already have the
    status string (from a fresh ledger read) don't need to re-query.

    NOTE: does NOT check staleness. Callers that hold a ledger doc
    must run `_is_stale(doc["updated_at"])` themselves before using
    this, OR use `witness_modifier_for(source)` which handles both.
    """
    tier = (status or "").strip().upper()
    if tier == "TRUSTED":
        return _env_float("WITNESS_MODIFIER_TRUSTED", _DEFAULT_TRUSTED)
    if tier == "WATCHLIST":
        return _env_float("WITNESS_MODIFIER_WATCHLIST", _DEFAULT_WATCHLIST)
    if tier == "UNTRUSTED":
        return _env_float("WITNESS_MODIFIER_UNTRUSTED", _DEFAULT_UNTRUSTED)
    # Unknown tier — default hostile, no influence.
    return 0.0


async def witness_modifier_for(source: str) -> float:
    """Read the credibility ledger for `source`, return the tier ceiling.

    Default-hostile:
      * no ledger row     → 0.0
      * unknown status    → 0.0
      * read error        → 0.0
      * stale row         → 0.0  (updated_at older than
                                  WITNESS_STALE_MAX_HOURS, default 72h)
      * missing updated_at → 0.0 (treated as stale)

    The caller (Governor) must still verify orthogonality before
    applying this ceiling — this module returns the CAP, not a
    guaranteed modifier value.
    """
    try:
        doc = await db[EXTERNAL_SOURCE_CREDIBILITY].find_one(
            {"source": source}, {"_id": 0, "status": 1, "updated_at": 1},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "witness_influence: ledger read failed for source=%r: %r",
            source, e,
        )
        return 0.0
    if not doc:
        return 0.0
    if _is_stale(doc.get("updated_at")):
        logger.info(
            "witness_influence: stale credibility row for source=%r "
            "(updated_at=%r) — clamping to 0.0",
            source, doc.get("updated_at"),
        )
        return 0.0
    return modifier_for_status(doc.get("status"))


async def witness_influence_snapshot(sources: list[str]) -> dict[str, dict]:
    """Return a `{source: {status, modifier_cap, samples, stale}}` map
    for the admin panel + operator dashboards. Default-hostile per
    source: a missing ledger row surfaces as UNTRUSTED / 0.0 / 0
    samples so the operator can see the source is registered but
    earning nothing yet.

    Staleness (2026-02-19): rows whose `updated_at` is older than
    WITNESS_STALE_MAX_HOURS (default 72h) — or missing — return
    `stale=True` and `modifier_cap=0.0` regardless of tier. The
    original `status` is still surfaced so the operator can see WHY
    the modifier is clamped ("TRUSTED but stale").
    """
    out: dict[str, dict] = {}
    for source in sources:
        try:
            doc = await db[EXTERNAL_SOURCE_CREDIBILITY].find_one(
                {"source": source},
                {"_id": 0, "status": 1, "samples": 1, "wins": 1,
                 "losses": 1, "verified_alpha": 1, "orthogonal_win_rate": 1,
                 "updated_at": 1},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "witness_influence_snapshot: read failed for %r: %r",
                source, e,
            )
            doc = None
        if not doc:
            out[source] = {
                "status": "UNTRUSTED",
                "modifier_cap": 0.0,
                "samples": 0,
                "wins": 0,
                "losses": 0,
                "verified_alpha": 0.0,
                "orthogonal_win_rate": 0.0,
                "ledger_present": False,
                "updated_at": None,
                "stale": False,  # no row at all — not "stale", just absent
            }
        else:
            stale = _is_stale(doc.get("updated_at"))
            raw_cap = modifier_for_status(doc.get("status"))
            out[source] = {
                "status": doc.get("status") or "UNTRUSTED",
                # Effective cap after staleness clamp.
                "modifier_cap": 0.0 if stale else raw_cap,
                "samples": int(doc.get("samples") or 0),
                "wins": int(doc.get("wins") or 0),
                "losses": int(doc.get("losses") or 0),
                "verified_alpha": float(doc.get("verified_alpha") or 0.0),
                "orthogonal_win_rate": float(
                    doc.get("orthogonal_win_rate") or 0.0
                ),
                "ledger_present": True,
                "updated_at": doc.get("updated_at"),
                "stale": stale,
            }
    return out
