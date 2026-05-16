"""Lane-isolation regression guard.

Doctrine (2026-02-16):
    Equity and crypto execution paths are physically independent. This
    test stops any future code from quietly re-coupling them — e.g. a
    crypto-path module importing the equity-only executor seat helper,
    or an equity-path module reaching into the kraken adapter.

    Past bug class this guard targets:
        crypto path accidentally calls get_executor_holder()
        (equity-only) → crypto intents get stamped with the equity
        holder → gate chain blocks them with a misleading message
        pointing at the wrong brain.

    Doctrine locked here:
        - equity seat cannot execute crypto
        - crypto seat cannot depend on equity
        - lane authority stays lane-owned
"""
from pathlib import Path


# This file lives at /app/backend/tests/test_lane_isolation.py.
#   parents[0] = /app/backend/tests
#   parents[1] = /app/backend   ← the backend root we want
BACKEND = Path(__file__).resolve().parents[1]

CRYPTO_ROOTS = [
    BACKEND / "shared" / "crypto",
]

FORBIDDEN_IN_CRYPTO = [
    "from shared.executor_seat import get_executor_holder",
    "import shared.executor_seat",
    "from shared.equity",
    "shared.equity.",
    "alpaca_routes",
    "shared.exposure_caps import",
]

EQUITY_ROOTS = [
    BACKEND / "shared" / "equity",
]

FORBIDDEN_IN_EQUITY = [
    "from shared.crypto",
    "shared.crypto.",
    "crypto.broker_adapter",
    "kraken",
]


def _py_files(paths):
    for root in paths:
        if not root.exists():
            continue
        yield from root.rglob("*.py")


def test_crypto_lane_does_not_import_equity_authority():
    offenders = []

    for path in _py_files(CRYPTO_ROOTS):
        text = path.read_text(encoding="utf-8", errors="ignore")

        for forbidden in FORBIDDEN_IN_CRYPTO:
            if forbidden in text:
                offenders.append(f"{path}: forbidden `{forbidden}`")

    assert not offenders, "\n".join(offenders)


def test_equity_lane_does_not_import_crypto_authority():
    offenders = []

    for path in _py_files(EQUITY_ROOTS):
        text = path.read_text(encoding="utf-8", errors="ignore")

        for forbidden in FORBIDDEN_IN_EQUITY:
            if forbidden in text:
                offenders.append(f"{path}: forbidden `{forbidden}`")

    assert not offenders, "\n".join(offenders)


def test_crypto_modules_do_not_call_legacy_get_executor_holder():
    offenders = []

    for path in _py_files(CRYPTO_ROOTS):
        text = path.read_text(encoding="utf-8", errors="ignore")

        if "get_executor_holder(" in text:
            offenders.append(f"{path}: legacy get_executor_holder call in crypto path")

    assert not offenders, "\n".join(offenders)
