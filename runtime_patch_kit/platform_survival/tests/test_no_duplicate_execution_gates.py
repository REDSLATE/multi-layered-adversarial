"""Brain-stack tripwire — fails the sidecar CI if any old per-platform /
per-deploy execution-gate logic creeps back. Doctrine: the brain SIDECAR
never holds execution authority. MC is the canonical gate, every order
requires an MC-signed receipt.

Scoped to the backend root so the scan is platform/cwd-independent.
"""
from pathlib import Path

FORBIDDEN = [
    "local_execution_authority = True",
    '"local_execution_authority": true',
    "may_execute = True",
    "can_execute = True",
    "if live_enabled",
    "if paper_only",
    "if observe_only",
    "operator_lock_default",
]

ALLOWLIST = {
    # Adjust if your stack copies the module elsewhere.
    "services/platform_survival/__init__.py",
    "tests/test_no_duplicate_execution_gates.py",
}

# Anchor to the backend root (this file lives at backend/tests/...).
BACKEND_ROOT = Path(__file__).resolve().parent.parent


def test_no_duplicate_execution_gate_logic():
    offenders = []

    for path in BACKEND_ROOT.rglob("*.py"):
        rel = str(path.relative_to(BACKEND_ROOT))
        if rel in ALLOWLIST or ".venv" in rel or "__pycache__" in rel:
            continue

        text = path.read_text(errors="ignore")

        for token in FORBIDDEN:
            if token in text:
                offenders.append((rel, token))

    assert not offenders, f"Duplicate/old gate logic found: {offenders}"
