"""CI-enforced contract: DOCTRINE_CARDS must stay in sync with the
doctrine functions they describe.

Catches:
  * Renamed snapshot fields (function updated, card stale)
  * Phantom fields (card claims a field the function never references)
  * Dead cards (card present but no _DOCTRINE_FN_MAP entry)
  * Schema regressions (missing required card fields)
"""
import inspect
import re
from typing import Any, Callable, Dict

import pytest

import shared.doctrine.brain_sidecars as generic_mod
import shared.doctrine.large_cap_doctrine as lcap_mod
import shared.doctrine.strategy_doctrines as gng_mod
from shared.doctrine.brain_sidecars import (
    DOCTRINE_CARDS as GENERIC_CARDS,
    _DOCTRINE_FN_MAP as GENERIC_FN_MAP,
)
from shared.doctrine.large_cap_doctrine import (
    DOCTRINE_CARDS as LCAP_CARDS,
    _DOCTRINE_FN_MAP as LCAP_FN_MAP,
)
from shared.doctrine.strategy_doctrines import (
    DOCTRINE_CARDS as GNG_CARDS,
    _DOCTRINE_FN_MAP as GNG_FN_MAP,
)


ALL_CARDS: Dict[str, Dict[str, Any]] = {
    **GNG_CARDS,
    **LCAP_CARDS,
    **GENERIC_CARDS,
}
ALL_FN_MAPS: Dict[str, str] = {
    **GNG_FN_MAP,
    **LCAP_FN_MAP,
    **GENERIC_FN_MAP,
}


def _resolve_function(fn_name: str, strategy_id: str) -> Callable:
    for mod in (gng_mod, lcap_mod, generic_mod):
        if hasattr(mod, fn_name):
            return getattr(mod, fn_name)
    pytest.fail(
        f"Strategy '{strategy_id}': function '{fn_name}' not found in any "
        f"doctrine module"
    )


def test_all_cards_have_functions():
    """Every card must wire to a function via _DOCTRINE_FN_MAP."""
    for sid in ALL_CARDS:
        assert sid in ALL_FN_MAPS, (
            f"Strategy '{sid}': DOCTRINE_CARDS entry exists but no "
            f"_DOCTRINE_FN_MAP mapping. Add: '{sid}': '<function_name>'"
        )


def test_all_mapped_functions_exist():
    """Every function name in _DOCTRINE_FN_MAP must resolve and be callable."""
    for sid, fn_name in ALL_FN_MAPS.items():
        fn = _resolve_function(fn_name, sid)
        assert callable(fn), f"Strategy '{sid}': '{fn_name}' is not callable"


def test_snapshot_fields_read_match_function_source():
    """Card-claimed snapshot fields must appear in function source."""
    for sid, card in ALL_CARDS.items():
        fn_name = ALL_FN_MAPS[sid]
        fn = _resolve_function(fn_name, sid)
        src = inspect.getsource(fn)
        for field in card.get("snapshot_fields_read", []):
            found = f'"{field}"' in src or f"'{field}'" in src
            assert found, (
                f"\nStrategy '{sid}' (function '{fn_name}'):\n"
                f"  Card claims to read snapshot field '{field}',\n"
                f"  but this string does not appear in the function source.\n"
                f"  Either the field was renamed in the function, or the "
                f"card is stale."
            )


def test_risk_flags_read_match_function_source():
    """Card-claimed risk flags / labels must appear in function source."""
    for sid, card in ALL_CARDS.items():
        fn_name = ALL_FN_MAPS[sid]
        fn = _resolve_function(fn_name, sid)
        src = inspect.getsource(fn)
        for flag in card.get("risk_flags_read", []):
            found = f'"{flag}"' in src or f"'{flag}'" in src
            assert found, (
                f"\nStrategy '{sid}' (function '{fn_name}'):\n"
                f"  Card claims to read risk flag '{flag}',\n"
                f"  but this string does not appear in the function source."
            )


def test_no_duplicate_strategy_ids():
    """Strategy IDs must be globally unique across doctrine modules."""
    seen = set()
    sources = [
        ("strategy_doctrines", GNG_CARDS),
        ("large_cap_doctrine", LCAP_CARDS),
        ("brain_sidecars", GENERIC_CARDS),
    ]
    for module_name, cards in sources:
        for sid in cards:
            assert sid not in seen, (
                f"Duplicate strategy_id '{sid}' detected in {module_name}"
            )
            seen.add(sid)


def test_all_cards_have_required_fields():
    """Schema enforcement so the dashboard never receives a half-built card."""
    required = [
        "title",
        "category",
        "lane",
        "tagline",
        "source_attribution",
        "doctrine_version",
        "ideal_conditions",
        "entries",
        "exits",
        "size_modifier_notes",
        "snapshot_fields_read",
        "risk_flags_read",
    ]
    for sid, card in ALL_CARDS.items():
        for field in required:
            assert field in card, (
                f"Strategy '{sid}': missing required field '{field}'"
            )
        assert isinstance(card["ideal_conditions"], list)
        assert isinstance(card["entries"], list)
        assert isinstance(card["exits"], list)
        assert isinstance(card["snapshot_fields_read"], list)
        assert isinstance(card["risk_flags_read"], list)


def test_doctrine_version_format():
    """Versions must match '<name>_v<int>' for traceability."""
    pattern = re.compile(r"^[a-z_]+_v\d+$")
    for sid, card in ALL_CARDS.items():
        ver = card["doctrine_version"]
        assert pattern.match(ver), (
            f"Strategy '{sid}': doctrine_version '{ver}' must match "
            f"'^[a-z_]+_v\\d+$' (e.g. 'gap_and_go_v1')"
        )
