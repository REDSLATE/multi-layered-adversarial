"""CI tripwire — fails the build if any old per-platform / per-deploy
execution-gate logic creeps back in. The doctrine pin: MC is the
canonical gate. No file other than `shared/runtime/platform_survival.py`
(and this test itself) may carry an alternate authority check.

Scoped to `/app/backend` so the scan doesn't pick up node_modules or
frontend.
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
    "shared/runtime/platform_survival.py",
    "tests/test_no_duplicate_execution_gates.py",
}

# Anchor to the backend root so the scan is platform/cwd-independent.
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
