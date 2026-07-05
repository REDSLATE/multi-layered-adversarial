"""One-shot migration — mirror `brain_roster.current.assignments`
into the canonical `seat_registry` collection (2026-02-17).

Doctrine (operator-pinned 2026-02-17):
    seat_registry           = PRIMARY authority for (lane, role) holders
    brain_roster            = VALID FALLBACK (roster UI writes here)
    shared_brain_roster     = DEAD NAMESPACE (do not read, do not write)
    crypto executor key     = "crypto"  (NOT "crypto_executor")

Why this script:
    The 2026-02-17 preview investigation found `seat_registry` empty
    while `brain_roster.current` was populated. The runtime resolver
    hits `seat_registry` first, so populating it makes the fast path
    the correct path — the roster fallback stays as a safety net for
    cases where `seat_registry` is stale or wiped.

Behavior:
    * Read `brain_roster.current.assignments`.
    * For each (lane, role) with a non-null holder, upsert the
      canonical `seat_registry` doc keyed `_id = "<lane>:<role>"`.
    * Never delete existing seat_registry rows the operator may have
      pinned directly — this is additive.
    * Idempotent: rerunning with the same roster is a no-op except
      for the `last_changed_at` timestamp bump.

Usage:
    python -m backend.scripts.migrate_brain_roster_to_seat_registry --dry-run
    python -m backend.scripts.migrate_brain_roster_to_seat_registry --apply

CLI intentionally requires --apply so you can eyeball the diff first.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Preserve absolute imports the way the backend runs — the writer path
# has to see `namespaces` and `db` the same way `shared/roster.py` does.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# Canonical assignment-key → (lane, role) reverse map. This mirrors the
# forward direction that lives in `shared/roster.py` — kept in sync by
# hand because the roster module doesn't currently export a reverse
# view.
_KEY_TO_LANE_ROLE: dict[str, tuple[str, str]] = {
    # equity — bare-role keys
    "strategist":       ("equity", "strategist"),
    "governor":         ("equity", "governor"),
    "executor":         ("equity", "executor"),
    "auditor":          ("equity", "auditor"),
    # crypto — canonical keys (crypto executor = "crypto")
    "crypto_strategist": ("crypto", "strategist"),
    "crypto_governor":   ("crypto", "governor"),
    "crypto":            ("crypto", "executor"),
    "crypto_auditor":    ("crypto", "auditor"),
    # crypto — tolerated legacy alias (only used if the canonical key is empty)
    "crypto_executor":   ("crypto", "executor"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seat_id(lane: str, role: str) -> str:
    return f"{lane}:{role}"


async def migrate(apply: bool) -> dict:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND_DIR / ".env")

    from db import db  # noqa: WPS433 — needs env loaded first
    from namespaces import BRAIN_ROSTER  # noqa: WPS433

    roster = await db[BRAIN_ROSTER].find_one({"_id": "current"})
    if not roster:
        return {"ok": False, "reason": "brain_roster.current missing", "written": 0}

    assignments: dict = (roster.get("assignments") or {})

    # Collapse legacy alias → canonical if canonical is empty.
    if not assignments.get("crypto") and assignments.get("crypto_executor"):
        assignments["crypto"] = assignments["crypto_executor"]

    plan: list[dict] = []
    for key, (lane, role) in _KEY_TO_LANE_ROLE.items():
        if key == "crypto_executor":
            continue  # already collapsed into "crypto"
        holder = assignments.get(key)
        if not holder:
            continue
        plan.append({
            "seat_id": _seat_id(lane, role),
            "lane": lane,
            "role": role,
            "holder": holder,
            "assignment_key": key,
        })

    if not plan:
        return {"ok": True, "reason": "no non-null holders in roster", "written": 0}

    if not apply:
        return {"ok": True, "dry_run": True, "plan": plan, "written": 0}

    now = _now_iso()
    written = 0
    for item in plan:
        upsert = {
            "$set": {
                "holder": item["holder"],
                "since": now,
                "assigned_by": "migrate_brain_roster_to_seat_registry",
                "reason": f"one-shot sync from brain_roster.{item['assignment_key']}",
                "last_changed_at": now,
            }
        }
        await db["seat_registry"].update_one(
            {"_id": item["seat_id"]}, upsert, upsert=True,
        )
        written += 1

    return {"ok": True, "written": written, "plan": plan}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="Actually write to seat_registry. Without this, dry-run only.")
    p.add_argument("--dry-run", action="store_true",
                   help="Explicit dry-run (default when --apply is absent).")
    args = p.parse_args()

    result = asyncio.run(migrate(apply=bool(args.apply)))
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
