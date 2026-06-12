"""OTOCO live tile — open-orders grouping logic tests (2026-02-19).

Tests `_group_open_orders_by_combo` in isolation so the grouper logic
is pinned without dragging in the Webull SDK / live HTTP path.

Doctrine pin: the operator-facing tile keys brackets on Webull's
`client_combo_order_id` (mirrored to each leg). Leg kind is inferred
from `combo_type=MASTER` for the entry; `client_order_id` prefixes
(`tp-`, `sl-`, `mc-otoco-`) for the OCO children. Standalone orders
(combo_type=NORMAL or missing combo_id) are returned separately so
the operator still sees them.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from routes.webull_admin import (  # noqa: E402
    _classify_leg,
    _group_open_orders_by_combo,
)


def test_classify_leg_master_by_combo_type():
    assert _classify_leg("mc-otoco-abc123", "MASTER") == "master"
    assert _classify_leg("anything", "MASTER") == "master"


def test_classify_leg_master_by_prefix():
    # Fallback when combo_type is missing (some SDK responses).
    assert _classify_leg("mc-otoco-xyz", "") == "master"


def test_classify_leg_tp_sl_by_prefix():
    assert _classify_leg("tp-mc-otoco-abc123", "OTOCO") == "tp"
    assert _classify_leg("sl-mc-otoco-abc123", "OTOCO") == "sl"


def test_classify_leg_unknown_otoco_child():
    """OTOCO leg without our tp-/sl- prefix → surface as unknown_otoco_child
    so the operator can see it without us mislabeling."""
    assert (
        _classify_leg("3rdparty-id-456", "OTOCO") == "unknown_otoco_child"
    )


def test_classify_leg_standalone():
    assert _classify_leg("random-order", "NORMAL") == "standalone"
    assert _classify_leg("", "") == "standalone"


# ── Grouper tests ─────────────────────────────────────────────────────


def _row(
    coid: str, combo_id: str, combo_type: str, order_type: str,
    *, symbol="AAL", side="BUY", status="WORKING", create_time="2026-02-19T10:00:00Z",
    limit_price=None, stop_price=None,
) -> dict:
    return {
        "client_order_id": coid,
        "client_combo_order_id": combo_id,
        "combo_type": combo_type,
        "order_type": order_type,
        "symbol": symbol,
        "side": side,
        "status": status,
        "create_time": create_time,
        "quantity": "1",
        "filled_quantity": "0",
        "limit_price": limit_price,
        "stop_price": stop_price,
        "order_id": f"WB-{coid[:6]}",
    }


def test_groups_three_legs_into_one_bracket():
    rows = [
        _row("mc-otoco-A1", "combo-A", "MASTER", "MARKET"),
        _row("tp-mc-otoco-A1", "combo-A", "OTOCO", "LIMIT", limit_price="16.50"),
        _row("sl-mc-otoco-A1", "combo-A", "OTOCO", "STOP", stop_price="14.00"),
    ]
    out = _group_open_orders_by_combo(rows)
    assert len(out["brackets"]) == 1
    assert out["standalone"] == []
    b = out["brackets"][0]
    assert b["combo_id"] == "combo-A"
    assert b["symbol"] == "AAL"
    assert b["master"]["client_order_id"] == "mc-otoco-A1"
    assert b["tp"]["client_order_id"] == "tp-mc-otoco-A1"
    assert b["tp"]["limit_price"] == "16.50"
    assert b["sl"]["client_order_id"] == "sl-mc-otoco-A1"
    assert b["sl"]["stop_price"] == "14.00"
    assert b["other_legs"] == []


def test_multiple_brackets_stay_separated():
    rows = [
        _row("mc-otoco-A1", "combo-A", "MASTER", "MARKET", symbol="AAL"),
        _row("tp-mc-otoco-A1", "combo-A", "OTOCO", "LIMIT", symbol="AAL"),
        _row("sl-mc-otoco-A1", "combo-A", "OTOCO", "STOP", symbol="AAL"),
        _row("mc-otoco-B2", "combo-B", "MASTER", "MARKET", symbol="PFE"),
        _row("tp-mc-otoco-B2", "combo-B", "OTOCO", "LIMIT", symbol="PFE"),
        _row("sl-mc-otoco-B2", "combo-B", "OTOCO", "STOP", symbol="PFE"),
    ]
    out = _group_open_orders_by_combo(rows)
    assert len(out["brackets"]) == 2
    symbols = sorted(b["symbol"] for b in out["brackets"])
    assert symbols == ["AAL", "PFE"]
    for b in out["brackets"]:
        assert b["master"] and b["tp"] and b["sl"]


def test_standalone_orders_separated():
    rows = [
        _row("mc-otoco-A1", "combo-A", "MASTER", "MARKET"),
        _row("tp-mc-otoco-A1", "combo-A", "OTOCO", "LIMIT"),
        _row("sl-mc-otoco-A1", "combo-A", "OTOCO", "STOP"),
        _row("plain-order-1", "", "NORMAL", "MARKET", symbol="MSFT"),
        _row("plain-order-2", "", "", "LIMIT", symbol="GOOG"),
    ]
    out = _group_open_orders_by_combo(rows)
    assert len(out["brackets"]) == 1
    assert len(out["standalone"]) == 2
    standalone_syms = sorted(r["symbol"] for r in out["standalone"])
    assert standalone_syms == ["GOOG", "MSFT"]


def test_partial_bracket_with_missing_leg():
    """If Webull returns a bracket with only 2 of 3 legs (e.g., the
    master already filled and is no longer in the open list), the
    grouper still surfaces what it has."""
    rows = [
        _row("tp-mc-otoco-A1", "combo-A", "OTOCO", "LIMIT", limit_price="16.50"),
        _row("sl-mc-otoco-A1", "combo-A", "OTOCO", "STOP", stop_price="14.00"),
    ]
    out = _group_open_orders_by_combo(rows)
    assert len(out["brackets"]) == 1
    b = out["brackets"][0]
    assert b["master"] is None
    assert b["tp"] is not None
    assert b["sl"] is not None


def test_camelCase_field_names_tolerated():
    """The SDK occasionally returns camelCase; the grouper must tolerate
    both shapes without missing the combo grouping."""
    rows = [
        {
            "clientOrderId": "mc-otoco-Q9",
            "clientComboOrderId": "combo-Q",
            "comboType": "MASTER",
            "orderType": "MARKET",
            "symbol": "AAL",
            "side": "BUY",
            "status": "WORKING",
            "createTime": "2026-02-19T10:00:00Z",
            "orderId": "WB-XYZ",
        },
        {
            "clientOrderId": "tp-mc-otoco-Q9",
            "clientComboOrderId": "combo-Q",
            "comboType": "OTOCO",
            "orderType": "LIMIT",
            "symbol": "AAL",
            "side": "SELL",
            "status": "WORKING",
            "limitPrice": "20.00",
        },
    ]
    out = _group_open_orders_by_combo(rows)
    assert len(out["brackets"]) == 1
    b = out["brackets"][0]
    assert b["combo_id"] == "combo-Q"
    assert b["master"]["client_order_id"] == "mc-otoco-Q9"
    assert b["tp"]["limit_price"] == "20.00"


def test_handles_empty_input():
    out = _group_open_orders_by_combo([])
    assert out == {"brackets": [], "standalone": []}
