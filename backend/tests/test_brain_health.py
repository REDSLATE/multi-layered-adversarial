"""Brain-Health composite endpoint tripwires (2026-02-17).

Operator contract pinned in `/app/backend/routes/brain_health.py`:
  GET /api/admin/runtime/brain-health/{brain}     → composite for one brain
  GET /api/admin/runtime/brain-health             → fleet (all 4 brains)

These tripwires lock the contract so the tile + any future automated
alerter both read from a stable shape:
  1. Thresholds are returned IN the payload (no hidden source-of-truth)
  2. Seat-walk is LANE-SCOPED (governor × {equity, crypto})
  3. Read-only: no MongoDB writes, no broker keys served, no
     execution authority touched
  4. Verdict logic: green / degraded / dead per held-seat × freshness
"""
from __future__ import annotations

import inspect

import pytest

from routes import brain_health as mod


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── SOURCE-SCAN INVARIANTS ────────────────────────


def test_thresholds_pinned_in_module():
    """The doctrine thresholds must live as a top-level constant on
    the module so they're trivially auditable AND so the response
    payload returns them verbatim (one source of truth)."""
    assert hasattr(mod, "THRESHOLDS"), (
        "Brain-health THRESHOLDS constant missing. The operator "
        "contract requires thresholds be exposed in the response "
        "payload, not buried in helper functions."
    )
    t = mod.THRESHOLDS
    assert "checkin_max_age_s" in t
    assert "opinion_max_age_s" in t
    assert "seat_walk_max_age_s" in t
    # Sanity: thresholds are positive integers.
    for k, v in t.items():
        assert isinstance(v, int) and v > 0, (
            f"threshold {k} must be a positive int; got {v!r}"
        )
    # Documented defaults from the operator's contract (2026-02-17).
    # Looser values are OK as a deliberate operator change but not
    # a silent drift — re-run this test after any threshold edit.
    assert t["checkin_max_age_s"] == 300
    assert t["opinion_max_age_s"] == 900
    assert t["seat_walk_max_age_s"] == 1800


def test_lane_seats_includes_equity_and_crypto():
    """Seat-walk MUST cover both lanes for every role. A brain that
    holds equity_governor but NOT crypto_governor must show a fresh
    equity walk and a null crypto walk — operator's explicit ask."""
    lanes = mod._LANE_SEATS
    for role in ("strategist", "executor", "governor", "auditor"):
        assert role in lanes, (
            f"role {role!r} missing from _LANE_SEATS — seat-walk "
            "would not include this role at all."
        )
        cell = lanes[role]
        assert "equity" in cell and "crypto" in cell, (
            f"role {role!r} must map both 'equity' and 'crypto' lanes; "
            f"got {sorted(cell)!r}"
        )
        # Crypto seat names follow the `crypto_*` convention or are
        # the bare `crypto` seat (operator-assigned crypto executor).
        crypto_seat = cell["crypto"]
        assert crypto_seat == "crypto" or crypto_seat.startswith("crypto_"), (
            f"crypto-lane seat for role {role!r} should be a crypto-* "
            f"name; got {crypto_seat!r}"
        )


def test_routes_are_read_only():
    """Source-scan: brain-health module MUST NEVER call any write
    operation on the database. It joins existing collections, never
    creates rows."""
    src = inspect.getsource(mod)
    forbidden = (
        ".insert_one(", ".insert_many(",
        ".update_one(", ".update_many(",
        ".replace_one(",
        ".delete_one(", ".delete_many(",
        ".find_one_and_update(",
        ".find_one_and_replace(",
        ".find_one_and_delete(",
    )
    for tok in forbidden:
        assert tok not in src, (
            f"DOCTRINE VIOLATION: brain_health.py contains {tok!r}. "
            "This endpoint is read-only by contract; any write means "
            "an operator dashboard query is mutating state."
        )


def test_no_broker_key_serving():
    """Defence in depth: this endpoint must never even reference broker
    key environment variables. Adjacent to the market-data-key proxy
    doctrine: broker keys NEVER leave MC's process memory."""
    src = inspect.getsource(mod)
    for forbidden in (
        "ALPACA_API_KEY", "ALPACA_SECRET",
        "KRAKEN_API_KEY", "KRAKEN_SECRET",
        "IBKR_TOKEN", "BROKER_SECRET",
    ):
        assert forbidden not in src, (
            f"DOCTRINE VIOLATION: brain_health.py references broker "
            f"key {forbidden!r}. The composite endpoint serves "
            "observability data only; broker keys must never be "
            "joined into operator-facing payloads."
        )


def test_doctrine_string_is_operator_read_only():
    """The response payload's `doctrine` field must mark this as a
    read-only composite so downstream consumers (LLM summarisers,
    alerters) cannot mistake it for a control-plane endpoint."""
    src = inspect.getsource(mod)
    assert "operator_read_only_composite" in src, (
        "Doctrine string 'operator_read_only_composite' missing — "
        "endpoint contract must self-identify as read-only."
    )


# ──────────────────────── VERDICT LOGIC INVARIANTS ────────────────────────


def _stub_seat_walk(holds: list[tuple[str, str]] | None = None) -> dict:
    """Build a seat_walk payload with the given (role, lane) cells held
    fresh and everything else null. Used by verdict tests."""
    holds = holds or []
    out: dict = {}
    for role in mod._ROLES:
        out[role] = {}
        for lane in mod._LANES:
            if (role, lane) in holds:
                out[role][lane] = {
                    "ts": "2026-02-17T00:00:00+00:00",
                    "age_sec": 30.0, "stale": False,
                    "mode": "DTD",
                    "seat": mod._LANE_SEATS[role][lane],
                }
            else:
                out[role][lane] = None
    return out


def test_verdict_green_when_all_healthy():
    """Fresh checkin + fresh opinion + at least one fresh held seat
    must produce green."""
    checkin = {"verdict": "prod", "age_sec": 30.0}
    opinion = {"silent": False, "age_sec": 30.0}
    seat_walk = _stub_seat_walk([("governor", "equity")])
    v = mod._compute_overall(checkin, opinion, seat_walk)
    assert v["verdict"] == "green", v
    assert v["reasons"] == []
    # Thresholds echoed for the tile.
    assert v["thresholds"] == mod.THRESHOLDS


def test_verdict_dead_when_never_checked_in():
    """A brain that has never posted a checkin is DEAD regardless of
    any other signal — operator can't trust any downstream data."""
    checkin = {"verdict": "never", "age_sec": None}
    opinion = {"silent": False, "age_sec": 30.0}
    seat_walk = _stub_seat_walk([("governor", "equity")])
    v = mod._compute_overall(checkin, opinion, seat_walk)
    assert v["verdict"] == "dead", v
    assert "checkin_never" in v["reasons"]


def test_verdict_dead_when_checkin_far_past_threshold():
    """Checkin age past 6× threshold (30min) is dead, not merely degraded."""
    checkin = {"verdict": "prod", "age_sec": mod.THRESHOLDS["checkin_max_age_s"] * 6 + 1}
    opinion = {"silent": False, "age_sec": 30.0}
    seat_walk = _stub_seat_walk([("governor", "equity")])
    v = mod._compute_overall(checkin, opinion, seat_walk)
    assert v["verdict"] == "dead", v


def test_verdict_degraded_on_opinion_silence_when_seated():
    """A seated brain that's opinion-silent is degraded (not dead)."""
    checkin = {"verdict": "prod", "age_sec": 30.0}
    opinion = {"silent": True, "age_sec": 5000.0}
    seat_walk = _stub_seat_walk([("governor", "equity")])
    v = mod._compute_overall(checkin, opinion, seat_walk)
    assert v["verdict"] == "degraded", v
    assert any("opinion_silent" in r for r in v["reasons"])


def test_verdict_ignores_opinion_silence_when_no_seats_held():
    """Operator's explicit doctrine: a brain with NO held seats has
    nothing to opine about. Opinion-silence must not flag it."""
    checkin = {"verdict": "prod", "age_sec": 30.0}
    opinion = {"silent": True, "age_sec": 99999.0}
    seat_walk = _stub_seat_walk([])  # NO seats held
    v = mod._compute_overall(checkin, opinion, seat_walk)
    assert v["verdict"] == "green", (
        "A seatless brain must not be flagged as degraded for being "
        "opinion-silent — there's no seat for it to opine on. "
        f"got reasons={v['reasons']!r}"
    )


def test_verdict_degraded_on_stale_held_seat_walk():
    """A held seat that has gone stale must surface in reasons. A
    NULL (unheld) cell on the same role must NOT cause a stale flag."""
    checkin = {"verdict": "prod", "age_sec": 30.0}
    opinion = {"silent": False, "age_sec": 30.0}
    # Held equity, crypto null; held cell is stale.
    seat_walk = {
        "strategist": {"equity": None, "crypto": None},
        "executor":   {"equity": None, "crypto": None},
        "governor":   {
            "equity": {
                "ts": "...", "age_sec": 9999.0, "stale": True,
                "mode": "DTD", "seat": "governor",
            },
            "crypto": None,  # not seated → must not flag
        },
        "auditor": {"equity": None, "crypto": None},
    }
    v = mod._compute_overall(checkin, opinion, seat_walk)
    assert v["verdict"] == "degraded", v
    assert any("governor_equity_stale" in r for r in v["reasons"])
    # Critical: must NOT have flagged the null crypto cell.
    assert not any("governor_crypto" in r for r in v["reasons"]), (
        "Null seat cells must not generate reasons — they mean "
        "'not seated' and should render as dimmed dots, not red."
    )


def test_verdict_thresholds_always_returned():
    """Every verdict (green, degraded, dead) must carry the thresholds
    in its payload so the tile + alerter never have to import the
    constant separately."""
    for checkin, opinion, seat_walk in (
        ({"verdict": "prod", "age_sec": 30.0},
         {"silent": False, "age_sec": 30.0},
         _stub_seat_walk([("governor", "equity")])),
        ({"verdict": "never", "age_sec": None},
         {"silent": True, "age_sec": None},
         _stub_seat_walk([])),
        ({"verdict": "prod", "age_sec": 30.0},
         {"silent": True, "age_sec": 5000.0},
         _stub_seat_walk([("governor", "equity")])),
    ):
        v = mod._compute_overall(checkin, opinion, seat_walk)
        assert v["thresholds"] == mod.THRESHOLDS, (
            f"thresholds dropped from {v['verdict']!r} payload"
        )


# ──────────────────────── ROUTE-WIRE INVARIANTS ────────────────────────


def test_routes_registered():
    """Both endpoints must be registered on the router with the
    documented paths."""
    paths = {r.path for r in mod.router.routes}
    assert "/admin/runtime/brain-health/{brain}" in paths, paths
    assert "/admin/runtime/brain-health" in paths, paths


def test_routes_require_admin_jwt():
    """Both endpoints must be guarded by `get_current_user` admin auth.
    A composite that joins sidecar identity + seat-walk timestamps
    should not be public-readable."""
    src = inspect.getsource(mod.get_brain_health)
    assert "get_current_user" in src
    src_list = inspect.getsource(mod.list_brain_health)
    assert "get_current_user" in src_list


def test_seat_walk_consults_current_roster():
    """The seat-walk gather MUST filter cells by the brain's CURRENT
    roster assignments. A historical walk for a seat the brain no
    longer holds must NOT surface — operator contract 2026-02-17.

    Source-scan: the gather function must call `get_roster` and use
    `assignments` to decide which cells to populate.
    """
    src = inspect.getsource(mod._gather_seat_walk)
    assert "get_roster" in src, (
        "DOCTRINE VIOLATION: _gather_seat_walk must read the current "
        "roster to decide which (role, lane) cells the brain holds. "
        "Without this filter, a Camaro that was briefly executor will "
        "forever show stale executor walks even after being moved "
        "back to strategist — which masks the half-dead-governor bug "
        "this endpoint was built to catch."
    )
    assert "assignments" in src, (
        "DOCTRINE VIOLATION: _gather_seat_walk must compare roster "
        "assignments against the brain to filter held seats."
    )
    # And no cell should be populated without first checking held_seats.
    assert "held_seats" in src, (
        "DOCTRINE VIOLATION: _gather_seat_walk must build a "
        "`held_seats` set and skip non-held seats — historical "
        "walks must be filtered, not surfaced as stale dots."
    )
